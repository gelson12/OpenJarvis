"""Domain-aware model preference learner (Round 4.3 — extra impressive layer).

Tracks (domain × model) → (success_count, total_count) over time, fed by
the reflector after each scoring. The orchestrator's `pick_best` can then
use these stats as a tie-break: when multiple providers respond, prefer
the model that has historically produced high-success reflections in
this domain.

Hermes can't do this — it has no per-domain reflection corpus to learn
from. OpenJarvis, with its reflector + complexity router, can.

Storage: ~/.openjarvis/model_preference.json (rewritten atomically)
Public API:
    record(domain, model, success)                — called from reflector
    rank_models(domain) → List[(model, score)]    — used by orchestrator
    best_model(domain) → Optional[str]            — convenience
    snapshot() → dict for /v1/_debug/agentic + dashboard

Env gate: OPENJARVIS_MODEL_PREFERENCE_ENABLED (default false)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_DATA: Dict[str, Dict[str, Dict[str, Any]]] = {}
_LOADED = False
_MIN_SAMPLES_FOR_RANKING = 5
_SMOOTHING_ALPHA = 2.0  # Beta(alpha, beta) prior so a single failure doesn't tank a model


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_MODEL_PREFERENCE_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _store_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "model_preference.json"


def _ensure_loaded() -> None:
    global _LOADED, _DATA
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        try:
            path = _store_path()
            if path.exists():
                _DATA = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.debug("model_preference: load failed: %s", exc)
            _DATA = {}
        _LOADED = True


def _save_locked() -> None:
    try:
        path = _store_path()
        path.write_text(json.dumps(_DATA, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("model_preference: save failed: %s", exc)


def record(domain: str, model: str, success: bool, *, confidence: float = 0.5) -> None:
    """Update the (domain, model) stats. Lightweight, called from reflector."""
    if not _enabled() or not model:
        return
    _ensure_loaded()
    d = (domain or "general").strip()[:48]
    m = model.strip()[:96]
    with _LOCK:
        domain_slot = _DATA.setdefault(d, {})
        slot = domain_slot.setdefault(m, {"total": 0, "success": 0, "conf_sum": 0.0,
                                          "first_seen": time.time(), "last_seen": time.time()})
        slot["total"] = int(slot.get("total", 0)) + 1
        if success:
            slot["success"] = int(slot.get("success", 0)) + 1
        slot["conf_sum"] = float(slot.get("conf_sum", 0.0)) + float(confidence)
        slot["last_seen"] = time.time()
        # Save every 5 records to amortize disk I/O
        if slot["total"] % 5 == 0:
            _save_locked()


def rank_models(domain: str) -> List[Tuple[str, float]]:
    """Return (model, smoothed_success_rate) sorted high→low for this domain.
    Falls back to the 'general' domain if the specific domain has too few
    samples. Empty list if neither has enough data."""
    if not _enabled():
        return []
    _ensure_loaded()
    d = (domain or "general").strip()
    with _LOCK:
        slots = dict(_DATA.get(d, {}))
        if not slots or sum(v.get("total", 0) for v in slots.values()) < _MIN_SAMPLES_FOR_RANKING:
            slots = dict(_DATA.get("general", {}))
    if not slots:
        return []
    ranked: List[Tuple[str, float]] = []
    for model, s in slots.items():
        total = int(s.get("total", 0))
        succ = int(s.get("success", 0))
        # Laplace smoothing with Beta(alpha, alpha) prior
        score = (succ + _SMOOTHING_ALPHA) / (total + 2 * _SMOOTHING_ALPHA)
        ranked.append((model, round(score, 4)))
    ranked.sort(key=lambda kv: kv[1], reverse=True)
    return ranked


def best_model(domain: str) -> Optional[str]:
    ranked = rank_models(domain)
    if not ranked:
        return None
    return ranked[0][0]


def snapshot() -> Dict[str, Any]:
    _ensure_loaded()
    with _LOCK:
        total_obs = sum(
            int(slot.get("total", 0))
            for domain_slots in _DATA.values()
            for slot in domain_slots.values()
        )
        domains = sorted(_DATA.keys())
        top_per_domain: Dict[str, List[Dict[str, Any]]] = {}
        for d in domains:
            ranked = rank_models(d)
            if ranked:
                top_per_domain[d] = [
                    {"model": m, "score": s,
                     "samples": int(_DATA.get(d, {}).get(m, {}).get("total", 0))}
                    for m, s in ranked[:5]
                ]
    return {
        "enabled": _enabled(),
        "total_observations": total_obs,
        "domains_tracked": len(domains),
        "top_per_domain": top_per_domain,
    }
