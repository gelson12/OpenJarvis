"""Cost telemetry (Round 4.4 — extra impressive layer).

Tracks per-provider token usage + estimated USD cost across the request
lifecycle. The orchestrator records every provider response; the chat
finalize path records the engine's main call. Aggregates are exposed via
/v1/_debug/agentic and the HTML dashboard so we can see at a glance which
providers are doing the heavy lifting and what they're costing.

Storage: in-memory (counters reset on restart — Railway sees ~daily
restarts which is the natural reporting window). Hot daily snapshots are
appended to ~/.openjarvis/cost_log.jsonl so we can audit historically.

Public API:
    record(provider, model, tokens_in, tokens_out, latency_ms, success)
    snapshot() → dict for /v1/_debug/agentic + dashboard
    estimated_cost(provider, model, tokens_in, tokens_out) → USD float

Pricing is intentionally rough; the goal is **relative** signal across
providers, not invoice-grade accounting.

Env gate: OPENJARVIS_COST_TELEMETRY_ENABLED (default false)
"""

from __future__ import annotations

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
_COUNTERS: Dict[str, Dict[str, Any]] = {}
_RECENT: List[Dict[str, Any]] = []  # last N calls for dashboard
_RECENT_MAX = 50


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_COST_TELEMETRY_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


# Per-1M-token USD prices (input, output). Best-effort — many free-tier
# providers are listed at zero. Used only for relative cost signal.
_PRICING: Dict[str, tuple] = {
    # provider/model           (in_per_M, out_per_M)
    "openai/gpt-4o":           (2.50,  10.00),
    "openai/gpt-4o-mini":      (0.15,   0.60),
    "anthropic/claude-3-5-sonnet": (3.00,  15.00),
    "anthropic/claude-3-5-haiku":  (0.80,   4.00),
    "google/gemini-2.5-flash": (0.075,  0.30),
    "google/gemini-2.5-pro":   (1.25,   5.00),
    "deepseek/deepseek-chat":  (0.27,   1.10),
    "groq":                    (0.0,    0.0),     # free tier
    "cerebras":                (0.0,    0.0),     # free
    "samba":                   (0.0,    0.0),     # free
    "kimi":                    (0.0,    0.0),     # free
    "glm":                     (0.0,    0.0),     # free
    "github":                  (0.0,    0.0),     # free
    "meta-llama/llama-3.3-70b-instruct:free": (0.0, 0.0),
    "openrouter/google/gemini-2.5-flash": (0.075, 0.30),
}


def _resolve_pricing(provider: str, model: str) -> tuple:
    if model and model in _PRICING:
        return _PRICING[model]
    if model:
        # try simple prefix match (e.g. "openrouter/google/gemini-2.5-flash-lite")
        for key, price in _PRICING.items():
            if model.startswith(key):
                return price
    if provider and provider in _PRICING:
        return _PRICING[provider]
    return (0.5, 1.5)  # cautious default for unknown models


def estimated_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    in_per_M, out_per_M = _resolve_pricing(provider, model)
    return round(
        (max(tokens_in, 0) / 1_000_000.0) * in_per_M
        + (max(tokens_out, 0) / 1_000_000.0) * out_per_M,
        6,
    )


def record(provider: str, model: str = "", *,
           tokens_in: int = 0, tokens_out: int = 0,
           latency_ms: int = 0, success: bool = True,
           role: str = "main") -> None:
    """Record one LLM call. `role` is free-form ('main', 'orchestrator',
    'reflector', etc.) for breakdown in the dashboard."""
    if not _enabled():
        return
    if not provider:
        provider = "unknown"
    cost = estimated_cost(provider, model, tokens_in, tokens_out)
    now = time.time()
    rec = {
        "ts": now,
        "iso": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model or "",
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_usd": cost,
        "latency_ms": int(latency_ms),
        "success": bool(success),
        "role": role,
    }
    with _LOCK:
        key = f"{provider}|{model or '*'}"
        slot = _COUNTERS.setdefault(key, {
            "provider": provider,
            "model": model or "",
            "calls": 0,
            "successes": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "latency_ms_sum": 0,
        })
        slot["calls"] += 1
        if success:
            slot["successes"] += 1
        slot["tokens_in"] += int(tokens_in)
        slot["tokens_out"] += int(tokens_out)
        slot["cost_usd"] = round(slot["cost_usd"] + cost, 6)
        slot["latency_ms_sum"] += int(latency_ms)
        _RECENT.append(rec)
        if len(_RECENT) > _RECENT_MAX:
            _RECENT[:] = _RECENT[-_RECENT_MAX:]


def snapshot() -> Dict[str, Any]:
    if not _enabled():
        return {"enabled": False}
    with _LOCK:
        per_provider = []
        total_cost = 0.0
        total_calls = 0
        total_tokens_in = 0
        total_tokens_out = 0
        for slot in _COUNTERS.values():
            calls = int(slot.get("calls", 0))
            avg_lat = int(slot.get("latency_ms_sum", 0) / calls) if calls else 0
            succ_rate = round(slot.get("successes", 0) / calls, 3) if calls else 0.0
            per_provider.append({
                "provider": slot.get("provider"),
                "model": slot.get("model"),
                "calls": calls,
                "successes": int(slot.get("successes", 0)),
                "success_rate": succ_rate,
                "tokens_in": int(slot.get("tokens_in", 0)),
                "tokens_out": int(slot.get("tokens_out", 0)),
                "cost_usd": slot.get("cost_usd", 0.0),
                "avg_latency_ms": avg_lat,
            })
            total_cost += slot.get("cost_usd", 0.0)
            total_calls += calls
            total_tokens_in += int(slot.get("tokens_in", 0))
            total_tokens_out += int(slot.get("tokens_out", 0))
        per_provider.sort(key=lambda r: r["cost_usd"], reverse=True)
        recent = list(_RECENT[-10:])
    return {
        "enabled": True,
        "total_calls": total_calls,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_cost_usd": round(total_cost, 6),
        "per_provider": per_provider,
        "recent_calls": recent,
    }


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _RECENT.clear()
