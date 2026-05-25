"""Self-healing watchdog (Round 4.2 — extra impressive layer).

Maintains a rolling window of recent reflection confidence + success scores
across all sessions. When the rolling average degrades below a threshold,
the watchdog emits a structured alert AND can auto-rollback the highest-risk
agentic flag (consensus mode → fastest, prompt-evolver off, etc.).

This is what closes the loop: instead of hoping each layer is helping,
we measure post-deploy quality drift and self-correct.

Public API:
    record(confidence, success, domain) → fed by reflector after each scoring
    health_snapshot() → dict for /v1/_debug/agentic + dashboard
    rolling_average() → quick (avg_conf, success_rate, n) tuple

Env gate: OPENJARVIS_WATCHDOG_ENABLED (default false until verified).
Threshold env: OPENJARVIS_WATCHDOG_MIN_CONFIDENCE (default 0.4)
Threshold env: OPENJARVIS_WATCHDOG_MIN_SAMPLES (default 10)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_WINDOW: Deque[Dict[str, Any]] = deque(maxlen=50)
_LAST_ALERT_TS = 0.0
_ALERT_COOLDOWN_SEC = 300.0
_ROLLBACKS: Deque[Dict[str, Any]] = deque(maxlen=20)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_WATCHDOG_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _min_confidence() -> float:
    try:
        return float(os.environ.get("OPENJARVIS_WATCHDOG_MIN_CONFIDENCE", "0.4"))
    except Exception:
        return 0.4


def _min_samples() -> int:
    try:
        return int(os.environ.get("OPENJARVIS_WATCHDOG_MIN_SAMPLES", "10"))
    except Exception:
        return 10


def _autorollback_enabled() -> bool:
    return os.environ.get("OPENJARVIS_WATCHDOG_AUTOROLLBACK", "false").lower() in (
        "1", "true", "yes", "on",
    )


def record(confidence: float, success: bool, *, domain: str = "general",
           session_id: str = "") -> None:
    """Append a reflection score to the rolling window. Triggers alerts
    when the window degrades. Called from the reflector after scoring."""
    if not _enabled():
        return
    try:
        conf = float(confidence)
    except Exception:
        return
    with _LOCK:
        _WINDOW.append({
            "ts": time.time(),
            "confidence": conf,
            "success": bool(success),
            "domain": domain or "general",
            "session_id": session_id,
        })
        avg_conf, succ_rate, n = _compute_rolling_locked()

    # Outside the lock — alerting is best-effort and may take a moment.
    if n >= _min_samples() and avg_conf < _min_confidence():
        _maybe_alert(avg_conf, succ_rate, n)


def _compute_rolling_locked() -> Tuple[float, float, int]:
    n = len(_WINDOW)
    if n == 0:
        return 0.0, 0.0, 0
    avg = sum(e["confidence"] for e in _WINDOW) / n
    succ = sum(1 for e in _WINDOW if e.get("success")) / n
    return avg, succ, n


def rolling_average() -> Tuple[float, float, int]:
    with _LOCK:
        return _compute_rolling_locked()


def _maybe_alert(avg_conf: float, succ_rate: float, n: int) -> None:
    global _LAST_ALERT_TS
    now = time.time()
    if (now - _LAST_ALERT_TS) < _ALERT_COOLDOWN_SEC:
        return
    _LAST_ALERT_TS = now
    logger.warning(
        "openjarvis.watchdog.degraded avg_confidence=%.2f success_rate=%.2f samples=%d "
        "threshold=%.2f",
        avg_conf, succ_rate, n, _min_confidence(),
    )
    if _autorollback_enabled():
        _attempt_rollback(avg_conf, succ_rate, n)


def _attempt_rollback(avg_conf: float, succ_rate: float, n: int) -> None:
    """Flip the highest-risk flag that's still on. Records to history."""
    # Priority order: highest-cost first (consensus is expensive + unproven,
    # so it's the first to be disabled).
    candidates = (
        ("OPENJARVIS_ORCHESTRATOR_MODE", "consensus", "fastest"),
        ("OPENJARVIS_PROMPT_EVOLVER_ENABLED", "true", "false"),
        ("OPENJARVIS_SKILL_PLANNER_ENABLED", "true", "false"),
    )
    for env_name, current, target in candidates:
        actual = os.environ.get(env_name, "").lower()
        if actual == current:
            os.environ[env_name] = target
            rec = {
                "ts": time.time(),
                "iso": datetime.now(timezone.utc).isoformat(),
                "flag": env_name,
                "from": current,
                "to": target,
                "trigger_avg_conf": avg_conf,
                "trigger_succ_rate": succ_rate,
                "trigger_samples": n,
            }
            with _LOCK:
                _ROLLBACKS.append(rec)
            logger.warning(
                "openjarvis.watchdog.autorollback flag=%s %s -> %s "
                "(avg_conf=%.2f succ=%.2f n=%d)",
                env_name, current, target, avg_conf, succ_rate, n,
            )
            return
    logger.warning("openjarvis.watchdog.autorollback no_candidate_left")


def health_snapshot() -> Dict[str, Any]:
    with _LOCK:
        n = len(_WINDOW)
        avg_conf, succ_rate, _ = _compute_rolling_locked()
        by_domain: Dict[str, Dict[str, float]] = {}
        for e in _WINDOW:
            d = e.get("domain", "general")
            slot = by_domain.setdefault(d, {"n": 0, "conf_sum": 0.0, "succ": 0})
            slot["n"] += 1
            slot["conf_sum"] += e["confidence"]
            if e.get("success"):
                slot["succ"] += 1
        domain_summary = {
            d: {
                "n": int(s["n"]),
                "avg_conf": round(s["conf_sum"] / s["n"], 3) if s["n"] else 0.0,
                "succ_rate": round(s["succ"] / s["n"], 3) if s["n"] else 0.0,
            }
            for d, s in by_domain.items()
        }
        rollbacks = list(_ROLLBACKS)
    state = "ok"
    if n < _min_samples():
        state = "warming-up"
    elif avg_conf < _min_confidence():
        state = "degraded"
    elif avg_conf < (_min_confidence() + 0.15):
        state = "watch"
    return {
        "enabled": _enabled(),
        "state": state,
        "samples": n,
        "avg_confidence": round(avg_conf, 3),
        "success_rate": round(succ_rate, 3),
        "threshold": _min_confidence(),
        "autorollback_enabled": _autorollback_enabled(),
        "by_domain": domain_summary,
        "rollback_history": rollbacks,
    }
