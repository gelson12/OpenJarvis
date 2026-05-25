"""Reflection-driven temperature auto-tuner (Round 5 BONUS-B — outclass Hermes #2).

Maintains a per-domain temperature recommendation that adapts to recent
reflection scores:
  - confidence consistently HIGH but success LOW (refusals/wrong-but-confident)
    on creative-leaning domains  → raise temperature for more diversity
  - confidence consistently LOW on factual domains → lower temperature
    to favour determinism

Hermes uses a single global temperature; OpenJarvis can vary per (domain,
recent-quality) which compounds the benefit of having a domain classifier.

Math: rolling-20 EMA of (success, confidence) → mapped to [0.0, 0.9].
Each domain starts at a sane default (lower for factual, higher for creative).

Public API:
    record(domain, confidence, success)        — from reflector fan-out
    recommended(domain) -> float                — read from routes.py
    snapshot() -> dict                          — for /v1/_debug/agentic

Env gate: OPENJARVIS_TEMP_TUNER_ENABLED (default false).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_WINDOWS: Dict[str, Deque[Dict[str, Any]]] = {}
_RECOMMENDED: Dict[str, float] = {}
_LAST_UPDATE: Dict[str, float] = {}

_WINDOW_SIZE = 20
_MIN_SAMPLES = 5
_UPDATE_COOLDOWN = 30.0  # seconds — don't oscillate

_FACTUAL_DOMAINS = {"math", "geography", "history", "science", "language"}
_CREATIVE_DOMAINS = {"multistep", "general"}

_DEFAULTS = {
    "math": 0.05,
    "geography": 0.10,
    "history": 0.15,
    "science": 0.15,
    "language": 0.10,
    "code": 0.20,
    "logic": 0.10,
    "automation": 0.20,
    "multistep": 0.40,
    "general": 0.30,
}


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_TEMP_TUNER_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _default_for(domain: str) -> float:
    return _DEFAULTS.get(domain or "general", 0.30)


def record(domain: str, confidence: float, success: bool) -> None:
    if not _enabled():
        return
    d = (domain or "general").strip()
    try:
        c = float(confidence)
    except Exception:
        return
    now = time.time()
    with _LOCK:
        win = _WINDOWS.setdefault(d, deque(maxlen=_WINDOW_SIZE))
        win.append({"ts": now, "c": c, "s": bool(success)})
        if len(win) < _MIN_SAMPLES:
            return
        if (now - _LAST_UPDATE.get(d, 0)) < _UPDATE_COOLDOWN:
            return
        _LAST_UPDATE[d] = now
        avg_c = sum(e["c"] for e in win) / len(win)
        rate = sum(1 for e in win if e["s"]) / len(win)
        old = _RECOMMENDED.get(d, _default_for(d))
        new = _adjust(old, d, avg_c, rate)
        _RECOMMENDED[d] = new
    if abs(new - old) >= 0.05:
        logger.info(
            "openjarvis.temp_tuner.adjust domain=%s old=%.2f new=%.2f avg_conf=%.2f rate=%.2f",
            d, old, new, avg_c, rate,
        )


def _adjust(current: float, domain: str, avg_conf: float, success_rate: float) -> float:
    """Move temperature in the direction that should improve recent quality."""
    is_factual = domain in _FACTUAL_DOMAINS
    is_creative = domain in _CREATIVE_DOMAINS or domain == "code"

    delta = 0.0
    # Factual domains: low confidence → lower temperature for determinism
    if is_factual:
        if avg_conf < 0.6 or success_rate < 0.7:
            delta = -0.05
        elif avg_conf > 0.85 and success_rate > 0.9:
            delta = +0.02  # nudge up slightly, but stay low overall
    # Creative/code domains: high confidence but low success → raise temp
    # (means model is confidently wrong; needs more diversity)
    elif is_creative:
        if avg_conf > 0.75 and success_rate < 0.6:
            delta = +0.05
        elif avg_conf < 0.5:
            delta = -0.05  # struggling — try more deterministic instead

    new = max(0.0, min(0.9, current + delta))
    return round(new, 3)


def recommended(domain: str) -> float:
    """Return the current recommended temperature for this domain.
    Always safe to call — defaults to a domain-appropriate value if not tuned yet."""
    d = (domain or "general").strip()
    with _LOCK:
        return _RECOMMENDED.get(d, _default_for(d))


def snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": _enabled()}
    with _LOCK:
        out["recommended"] = dict(_RECOMMENDED)
        out["samples_per_domain"] = {d: len(w) for d, w in _WINDOWS.items()}
        out["defaults"] = dict(_DEFAULTS)
    return out
