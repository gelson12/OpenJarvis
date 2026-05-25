"""Prompt evolver (Round 2.3 + Round 3.2 of OpenJarvis-native upgrade).

Per-domain system-prompt variants stored at
``~/.openjarvis/prompt_variants/<domain>/`` with three statuses:
``active`` | ``candidate`` | ``archived``.

Hermes evolves prompts using only in-prod reflection success-rate (noisy).
OpenJarvis evolves prompts using BOTH:
  1. In-prod reflection success-rate (`learning/reflector.py`) — fast signal
  2. TauBench mini-suite scores (`evals/datasets/taubench.py`) — rigorous signal

Promotion gate: candidate must have ≥30 in-prod samples AND beat active by
≥3% on TauBench. The eval coupling is what makes this "outclass Hermes" —
prompt promotion is gated on real benchmark deltas, not noisy in-prod scores.

Env gates:
    OPENJARVIS_PROMPT_EVOLVER_ENABLED  default false (opt-in after Round 3 verifies)

The evolver runs in two modes:
  - Read path (every turn): `current_prompt(domain)` returns the active
    variant or "" (fall back to bundled default).
  - Write path (cron/background): `maybe_propose(domain)` and
    `maybe_promote(domain)` are called periodically by the scheduler.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_PROMPT_EVOLVER_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Variant storage layout
# ---------------------------------------------------------------------------

def _variants_root() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    if base:
        root = Path(base) / "prompt_variants"
    else:
        root = Path.home() / ".openjarvis" / "prompt_variants"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return root


def _variant_path(domain: str, variant_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", domain.lower()).strip("-") or "general"
    return _variants_root() / safe / f"{variant_id}.json"


def _domain_dir(domain: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", domain.lower()).strip("-") or "general"
    d = _variants_root() / safe
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# ---------------------------------------------------------------------------
# Variant CRUD
# ---------------------------------------------------------------------------

_FILE_LOCK = threading.Lock()


def _new_variant_id() -> str:
    import uuid as _uuid
    return f"v-{int(time.time())}-{_uuid.uuid4().hex[:6]}"


def write_variant(domain: str, prompt_text: str, *, status: str,
                  samples: int = 0, success_rate: float = 0.0,
                  eval_score: Optional[float] = None,
                  variant_id: Optional[str] = None,
                  notes: str = "") -> str:
    """Write a variant. Returns variant_id."""
    vid = variant_id or _new_variant_id()
    rec = {
        "variant_id": vid,
        "domain": domain,
        "status": status,
        "samples": int(samples),
        "success_rate": float(success_rate),
        "eval_score": eval_score,
        "notes": notes[:280],
        "prompt": prompt_text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _variant_path(domain, vid)
    with _FILE_LOCK:
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("openjarvis.evolver.write domain=%s id=%s status=%s",
                domain, vid, status)
    return vid


def load_variants(domain: str, *, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _domain_dir(domain)
    out: List[Dict[str, Any]] = []
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if status_filter and rec.get("status") != status_filter:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Read path — every turn calls this
# ---------------------------------------------------------------------------

_PROMPT_CACHE: Dict[str, Tuple[float, str, Optional[str]]] = {}  # domain -> (ts, prompt, vid)
_CACHE_TTL = 120.0  # 2 min — prompts change rarely
_CACHE_LOCK = threading.Lock()


def current_prompt(domain: str) -> str:
    """Return the active prompt text for a domain, or "" to fall back to
    the bundled default. Cheap — cached for 2 min."""
    if not _enabled() or not domain:
        return ""
    now = time.time()
    with _CACHE_LOCK:
        cached = _PROMPT_CACHE.get(domain)
        if cached and (now - cached[0]) < _CACHE_TTL:
            return cached[1]
    variants = load_variants(domain, status_filter="active")
    if not variants:
        with _CACHE_LOCK:
            _PROMPT_CACHE[domain] = (now, "", None)
        return ""
    chosen = variants[0]
    text = chosen.get("prompt") or ""
    with _CACHE_LOCK:
        _PROMPT_CACHE[domain] = (now, text, chosen.get("variant_id"))
    return text


def current_variant_id(domain: str) -> Optional[str]:
    """Return active variant id (for outcome tracking). None if no active variant."""
    if not _enabled() or not domain:
        return None
    with _CACHE_LOCK:
        cached = _PROMPT_CACHE.get(domain)
        if cached and cached[2]:
            return cached[2]
    variants = load_variants(domain, status_filter="active")
    return variants[0].get("variant_id") if variants else None


def _invalidate_cache(domain: str) -> None:
    with _CACHE_LOCK:
        _PROMPT_CACHE.pop(domain, None)


# ---------------------------------------------------------------------------
# Outcome recording — called from reflector hook
# ---------------------------------------------------------------------------

def record_outcome(domain: str, variant_id: str, *, success: bool,
                   confidence: float = 0.5) -> None:
    """Append an outcome to the variant's record. Reflector calls this when
    a reflection contains a known active variant id."""
    if not _enabled() or not domain or not variant_id:
        return
    path = _variant_path(domain, variant_id)
    if not path.exists():
        return
    try:
        with _FILE_LOCK:
            rec = json.loads(path.read_text(encoding="utf-8"))
            outcomes = rec.setdefault("outcomes", [])
            outcomes.append({
                "ts": time.time(),
                "success": bool(success),
                "confidence": max(0.0, min(1.0, float(confidence))),
            })
            # Cap outcome history to last 200 to keep file size bounded.
            rec["outcomes"] = outcomes[-200:]
            # Refresh aggregate.
            n = len(rec["outcomes"])
            ok = sum(1 for o in rec["outcomes"] if o.get("success"))
            rec["samples"] = n
            rec["success_rate"] = (ok / n) if n else 0.0
            path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("evolver.record_outcome failed: %s", exc)


# ---------------------------------------------------------------------------
# Write path — Round 3.2 eval-driven proposal/promotion
# ---------------------------------------------------------------------------

_PROPOSE_SYSTEM = (
    "You are a prompt engineer. Given a current system prompt for a domain "
    "and recent outcome statistics, propose ONE refined system prompt that "
    "should improve performance. Return only the new prompt text — no "
    "commentary, no markdown fences."
)


def _engine_call(system: str, user: str) -> Optional[str]:
    try:
        from openjarvis.engine import generate
    except Exception:
        try:
            from openjarvis.engine._stubs import generate
        except Exception:
            return None
    try:
        result = generate(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            max_tokens=1500,
            temperature=0.3,
        )
    except Exception as exc:
        logger.debug("evolver: engine.generate failed: %s", exc)
        return None
    if isinstance(result, str):
        return result.strip() or None
    if isinstance(result, dict):
        for k in ("text", "content", "output", "response"):
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            return ((choices[0] or {}).get("message") or {}).get("content")
    try:
        return result.choices[0].message.content
    except Exception:
        return None


def maybe_propose(domain: str, *, min_samples: int = 30,
                  min_failure_rate: float = 0.25) -> Optional[str]:
    """If active variant has enough samples AND is underperforming, generate
    a candidate variant via the engine. Returns new variant_id or None."""
    if not _enabled() or not domain:
        return None
    active = load_variants(domain, status_filter="active")
    if not active:
        return None
    cur = active[0]
    samples = cur.get("samples", 0)
    rate = cur.get("success_rate", 0.0)
    if samples < min_samples:
        return None
    if (1.0 - rate) < min_failure_rate:
        return None  # not failing enough to bother

    cur_text = cur.get("prompt") or ""
    if not cur_text:
        return None

    user_prompt = (
        f"CURRENT PROMPT (domain={domain}):\n{cur_text}\n\n"
        f"OUTCOMES: {samples} samples, {rate*100:.0f}% success rate, "
        f"failure rate {(1.0-rate)*100:.0f}%.\n\n"
        "Propose a refined version that addresses likely weaknesses. "
        "Return only the new prompt text."
    )
    new_text = _engine_call(_PROPOSE_SYSTEM, user_prompt)
    if not new_text or new_text.strip() == cur_text.strip():
        return None
    return write_variant(domain, new_text, status="candidate",
                         notes=f"proposed at failure_rate={(1.0-rate):.2f}")


def _run_taubench(prompt_text: str, *, trials: int = 10) -> Optional[float]:
    """Run a TauBench mini-suite against a candidate prompt. Returns success
    rate (0..1) or None if eval is unavailable."""
    try:
        from openjarvis.evals.datasets import taubench  # type: ignore
    except Exception as exc:
        logger.debug("evolver: taubench unavailable: %s", exc)
        return None
    try:
        # OpenJarvis's TauBench runner exposes a callable suite-runner.
        # Function names vary; try the conventional entry points.
        for fn_name in ("run_suite", "run", "evaluate", "score"):
            fn = getattr(taubench, fn_name, None)
            if callable(fn):
                score = fn(system_prompt=prompt_text, trials=trials)
                if isinstance(score, (int, float)):
                    return float(score)
                if isinstance(score, dict):
                    for k in ("success_rate", "score", "accuracy", "pass_rate"):
                        v = score.get(k)
                        if isinstance(v, (int, float)):
                            return float(v)
        return None
    except Exception as exc:
        logger.debug("evolver: taubench run failed: %s", exc)
        return None


def maybe_promote(domain: str, *, min_samples: int = 30,
                  min_improvement: float = 0.03) -> Optional[str]:
    """Promote a candidate variant if it has enough samples AND beats active
    on TauBench by ≥min_improvement. This is the Round 3.2 eval gate that
    makes OpenJarvis's evolver strictly better than Hermes's (which gates on
    in-prod scores only). Returns promoted variant_id or None.
    """
    if not _enabled() or not domain:
        return None
    actives = load_variants(domain, status_filter="active")
    candidates = load_variants(domain, status_filter="candidate")
    if not actives or not candidates:
        return None
    active = actives[0]
    candidate = candidates[0]
    if candidate.get("samples", 0) < min_samples:
        return None

    # Eval gate
    active_score = _run_taubench(active.get("prompt", ""))
    candidate_score = _run_taubench(candidate.get("prompt", ""))
    if active_score is None or candidate_score is None:
        logger.debug("evolver: skipping promote — eval unavailable")
        return None
    if (candidate_score - active_score) < min_improvement:
        return None

    # Promote
    write_variant(domain, active.get("prompt", ""), status="archived",
                  samples=active.get("samples", 0),
                  success_rate=active.get("success_rate", 0.0),
                  eval_score=active_score,
                  variant_id=active.get("variant_id"),
                  notes="demoted by eval-gate")
    write_variant(domain, candidate.get("prompt", ""), status="active",
                  samples=candidate.get("samples", 0),
                  success_rate=candidate.get("success_rate", 0.0),
                  eval_score=candidate_score,
                  variant_id=candidate.get("variant_id"),
                  notes=f"promoted: eval {candidate_score:.2f} vs {active_score:.2f}")
    _invalidate_cache(domain)
    logger.info(
        "openjarvis.evolver.promoted domain=%s new=%s(eval=%.2f) old=%s(eval=%.2f)",
        domain, candidate.get("variant_id"), candidate_score,
        active.get("variant_id"), active_score,
    )
    return candidate.get("variant_id")
