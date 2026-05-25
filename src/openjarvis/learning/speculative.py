"""Speculative parallel execution (Round 5 BONUS-A — outclass Hermes #1).

Fires the orchestrator ensemble AND the main engine in parallel from the
same starting line, then returns whichever wins. The loser is cancelled.

Why this beats Hermes: Hermes picks ONE path per call (model A or B,
never both racing). Speculative racing gives OpenJarvis a lower latency
floor because we get whichever pipeline happens to be warm/cheap right
now, instead of betting on a single one.

Cost trade: at most 2× LLM calls per turn. Mitigation: only enabled for
non-streaming, when complexity is in the "interesting" band (0.2-0.6) —
trivial queries don't need the second runner, hard ones need the
ensemble's consensus anyway.

Public API:
    async race(messages, *, engine, run_all, pick_best, model, domain) -> dict
        returns {"text": ..., "winner_path": "orchestrator|engine", ...}

Env gate: OPENJARVIS_SPECULATIVE_RACE_ENABLED (default false).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_SPECULATIVE_RACE_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _complexity_in_band(c: Optional[float]) -> bool:
    if c is None:
        return False
    try:
        f = float(c)
    except Exception:
        return False
    return 0.2 <= f <= 0.6


async def race(messages: List[Dict[str, Any]], *,
               engine: Any,
               run_all: Callable,
               pick_best: Callable,
               model: str,
               domain: str = "general",
               complexity: Optional[float] = None,
               max_tokens: int = 800,
               temperature: float = 0.2) -> Optional[Dict[str, Any]]:
    """Race orchestrator vs single-engine call. Returns winner dict or None."""
    if not _enabled():
        return None
    if not _complexity_in_band(complexity):
        return None  # not in band — let normal path handle

    t0 = time.time()

    async def _orchestrator_path() -> Dict[str, Any]:
        responses = await run_all(messages)
        if not responses:
            return {"text": "", "model": "orchestrator", "winner_path": "orchestrator"}
        best = pick_best(responses, domain=domain)
        best = dict(best or {})
        best["winner_path"] = "orchestrator"
        return best

    async def _engine_path() -> Dict[str, Any]:
        from openjarvis.core.types import Message, Role
        msgs = []
        for m in messages:
            role_v = m.get("role", "user")
            try:
                role = Role(role_v) if not isinstance(role_v, Role) else role_v
            except Exception:
                role = Role.USER
            msgs.append(Message(role=role, content=m.get("content", "")))
        # engine.generate is sync — wrap in to_thread to keep this async
        result = await asyncio.to_thread(
            engine.generate, msgs,
            model=model, max_tokens=max_tokens, temperature=temperature,
        )
        text = ""
        if isinstance(result, dict):
            text = result.get("content") or result.get("text") or ""
        elif isinstance(result, str):
            text = result
        return {"text": text, "model": model, "winner_path": "engine",
                "usage": (result.get("usage") if isinstance(result, dict) else {})}

    t_orch = asyncio.create_task(_orchestrator_path())
    t_eng = asyncio.create_task(_engine_path())

    try:
        done, pending = await asyncio.wait(
            {t_orch, t_eng},
            timeout=10.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as exc:
        logger.debug("speculative.race wait failed: %s", exc)
        return None

    winner: Optional[Dict[str, Any]] = None
    for t in done:
        try:
            result = t.result()
            text = (result or {}).get("text") or ""
            if text and "Error" not in text:
                winner = result
                break
        except Exception as exc:
            logger.debug("speculative.race task failed: %s", exc)

    # Cancel the loser; if no winner yet, wait briefly for the second result
    if not winner and pending:
        try:
            done2, _ = await asyncio.wait(pending, timeout=8.0,
                                          return_when=asyncio.FIRST_COMPLETED)
            for t in done2:
                try:
                    result = t.result()
                    text = (result or {}).get("text") or ""
                    if text and "Error" not in text:
                        winner = result
                        break
                except Exception:
                    continue
            pending = pending - done2
        except Exception:
            pass

    for t in pending:
        t.cancel()

    if not winner:
        return None
    winner["elapsed_ms"] = int((time.time() - t0) * 1000)
    logger.info(
        "openjarvis.speculative.race winner=%s elapsed=%dms model=%s",
        winner.get("winner_path"), winner["elapsed_ms"], winner.get("model"),
    )
    return winner


def snapshot() -> Dict[str, Any]:
    return {
        "enabled": _enabled(),
        "band": [0.2, 0.6],
        "note": "races orchestrator vs single-engine; first non-error wins",
    }
