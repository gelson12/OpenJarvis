"""Debug endpoint that exercises every agentic layer synchronously and
returns a structured JSON status report.

This is the "fast verification" path — instead of waiting for Railway log
delivery to surface async events, this endpoint runs each layer in-process
and reports what actually happened.

GET /v1/_debug/agentic  -> structured JSON with one section per layer.

Layers exercised (in order):
  1. env       — show all OPENJARVIS_*_ENABLED + tier model env values
  2. skills    — count auto-loaded TOML skills + sample names
  3. goal_tracker — write a test goal + read it back
  4. skill_planner — try matching a known query against the loaded skills
  5. reflector — fire a synchronous reflection call against a canned (Q, A)
  6. orchestrator — ping all configured providers + return winner
  7. distiller  — verify import + trigger constructor
  8. prompt_evolver — list active variant count per domain
  9. eval      — run a quick 3-trial TauBench sample if harness available

Each layer block is independent and isolates its own errors so one layer's
failure doesn't mask another's success.

Auth: protected by the same Basic Auth gate as other endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Per-layer probes — each returns a dict, never raises
# ---------------------------------------------------------------------------

_AGENTIC_FLAGS = (
    "OPENJARVIS_SKILLS_AUTOLOAD_ENABLED",
    "OPENJARVIS_ORCHESTRATOR_ENABLED",
    "OPENJARVIS_ORCHESTRATOR_MODE",
    "OPENJARVIS_DISTILLER_ONLINE_ENABLED",
    "OPENJARVIS_REFLECTOR_ENABLED",
    "OPENJARVIS_REFLECTOR_MODEL",
    "OPENJARVIS_GOALS_ENABLED",
    "OPENJARVIS_PROMPT_EVOLVER_ENABLED",
    "OPENJARVIS_SKILL_PLANNER_ENABLED",
    "OPENJARVIS_HOME",
)


def _probe_env() -> Dict[str, Any]:
    return {
        flag: (os.environ.get(flag) or "<unset>")
        for flag in _AGENTIC_FLAGS
    }


def _probe_skills(app_state: Any) -> Dict[str, Any]:
    """Count loaded skills + return sample names."""
    out: Dict[str, Any] = {"loaded": False, "count": 0, "sample": []}
    try:
        from openjarvis.skills.manager import SkillManager
        bus = getattr(app_state, "bus", None)
        if bus is None:
            try:
                from openjarvis.core.bus import EventBus
                bus = EventBus()
            except Exception as e:
                out["error"] = f"bus unavailable: {e}"
                return out
        # Prefer the cached singleton from app.state (populated by the
        # startup hook); fall back to a fresh discover so the probe still
        # works pre-cache.
        cached = getattr(app_state, "skill_manager", None)
        if cached is not None:
            mgr = cached
        else:
            mgr = SkillManager(bus)
            from pathlib import Path as _Path
            import openjarvis.skills as _skills_pkg
            _bundled = _Path(_skills_pkg.__file__).parent / "data"
            mgr.discover(paths=[_bundled])
        manifests: List[Any] = []
        for attr in ("get_all", "list_skills", "all_skills", "skills"):
            obj = getattr(mgr, attr, None)
            if callable(obj):
                try:
                    manifests = list(obj()) or []
                    if manifests:
                        break
                except Exception:
                    continue
            elif isinstance(obj, dict) and obj:
                manifests = list(obj.values())
                break
            elif isinstance(obj, list) and obj:
                manifests = list(obj)
                break
        out["loaded"] = True
        out["count"] = len(manifests)
        out["sample"] = [
            getattr(m, "name", None) or getattr(m, "skill_name", None) or "?"
            for m in manifests[:8]
        ]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_goal_tracker() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False, "round_trip_ok": False}
    try:
        from openjarvis.tools import goal_tracker as _gt
        out["enabled"] = _gt._enabled()
        if not out["enabled"]:
            out["note"] = "OPENJARVIS_GOALS_ENABLED is false"
            return out
        # Write a unique test goal
        text = f"debug-probe-{int(time.time())}"
        add_res = _gt.goal_add(text, due=None, priority="low")
        out["add_ok"] = bool(add_res.get("ok"))
        out["added_id"] = add_res.get("goal", {}).get("id")
        # Read it back
        list_res = _gt.goal_list("active", 50)
        ids = [g.get("id") for g in list_res.get("goals", [])]
        out["list_count"] = len(ids)
        out["round_trip_ok"] = out["added_id"] in ids
        # Tidy up — archive the test goal so we don't pollute production
        if out["added_id"]:
            _gt.goal_archive(out["added_id"])
        # Verify system_prompt_block
        block = _gt.active_goals_block(max_goals=3)
        out["system_prompt_block_len"] = len(block)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc().splitlines()[-6:]
    return out


def _probe_skill_planner() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False, "test_match": None}
    try:
        from openjarvis.agents import skill_planner as _sp
        out["enabled"] = _sp._enabled()
        if not out["enabled"]:
            out["note"] = "OPENJARVIS_SKILL_PLANNER_ENABLED is false"
            return out
        # Probe with a query that should match one of the bundled skills
        # ("backup-files" is bundled at src/openjarvis/skills/data/backup-files.toml).
        m = _sp.find_matching_skill("back up my files in the downloads folder")
        if m:
            out["test_match"] = {
                "skill_name": m.skill_name,
                "score": round(m.score, 3),
            }
        else:
            out["test_match"] = None
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc().splitlines()[-6:]
    return out


def _probe_reflector(app_state: Any) -> Dict[str, Any]:
    """Fire a synchronous reflection call to verify the engine path works."""
    out: Dict[str, Any] = {"enabled": False, "sync_test_ok": False}
    try:
        from openjarvis.learning import reflector as _ref
        out["enabled"] = _ref._enabled()
        if not out["enabled"]:
            out["note"] = "OPENJARVIS_REFLECTOR_ENABLED is false"
            return out
        # Run the engine call SYNCHRONOUSLY (not via reflect_async background).
        # Pass the engine explicitly so the probe works even if set_engine
        # hasn't fired yet (e.g. testing immediately after deploy).
        t0 = time.time()
        engine = getattr(app_state, "engine", None)
        raw = _ref._call_engine(
            _ref._REFLECT_SYSTEM,
            _ref._build_user_prompt(
                "What is 2+2?",
                "2+2 equals 4.",
                domain="debug-probe",
            ),
            engine=engine,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        if not raw:
            out["sync_test_ok"] = False
            out["note"] = "engine returned None — likely no auxiliary engine available"
            return out
        parsed = _ref._extract_json(raw)
        if not parsed:
            out["raw_snippet"] = (raw or "")[:200]
            out["note"] = "engine returned text but JSON parse failed"
            return out
        norm = _ref._normalize(parsed)
        out["sync_test_ok"] = True
        out["score"] = norm
        out["elapsed_ms"] = elapsed_ms
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc().splitlines()[-6:]
    return out


async def _probe_orchestrator() -> Dict[str, Any]:
    """Ping all configured LLM providers in parallel and report."""
    out: Dict[str, Any] = {"enabled": False, "responses": 0}
    try:
        if os.getenv("OPENJARVIS_ORCHESTRATOR_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
            out["note"] = "OPENJARVIS_ORCHESTRATOR_ENABLED is false"
            return out
        out["enabled"] = True
        out["mode"] = os.getenv("OPENJARVIS_ORCHESTRATOR_MODE", "fastest")
        from openjarvis.orchestrator.router import run_all as _orch_run_all
        from openjarvis.orchestrator.router import pick_best as _orch_pick_best
        from openjarvis.orchestrator import router as _orch_mod
        out["providers_enabled_flags"] = {
            k: bool(v) for k, v in getattr(_orch_mod, "ENABLED", {}).items()
        }
        t0 = time.time()
        responses = await _orch_run_all([
            {"role": "user", "content": "Say 'pong' and nothing else."},
        ])
        elapsed_ms = int((time.time() - t0) * 1000)
        clean = [r for r in (responses or []) if r and r.get("text") and "Error" not in r.get("text", "")]
        out["responses"] = len(responses or [])
        out["clean_responses"] = len(clean)
        out["elapsed_ms"] = elapsed_ms
        if responses:
            winner = _orch_pick_best(responses)
            out["winner"] = {
                "model": winner.get("model"),
                "text_preview": (winner.get("text") or "")[:120],
            }
            out["all_providers"] = [r.get("model") for r in responses]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc().splitlines()[-6:]
    return out


def _probe_distiller() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False, "import_ok": False}
    try:
        out["enabled"] = os.getenv("OPENJARVIS_DISTILLER_ONLINE_ENABLED", "false").lower() in (
            "1", "true", "yes", "on",
        )
        from openjarvis.learning.distillation.orchestrator import DistillationOrchestrator
        from openjarvis.learning.distillation.triggers import OnDemandTrigger
        out["import_ok"] = True
        out["orchestrator_class"] = DistillationOrchestrator.__name__
        out["trigger_class"] = OnDemandTrigger.__name__
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_prompt_evolver() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False}
    try:
        from openjarvis.prompt import evolver as _ev
        out["enabled"] = _ev._enabled()
        if not out["enabled"]:
            out["note"] = "OPENJARVIS_PROMPT_EVOLVER_ENABLED is false (opt-in)"
            return out
        # Count active variants in known domains
        domains = ("general", "voice", "code", "chat")
        out["active_variants"] = {
            d: len(_ev.load_variants(d, status_filter="active")) for d in domains
        }
        out["candidate_variants"] = {
            d: len(_ev.load_variants(d, status_filter="candidate")) for d in domains
        }
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_eval() -> Dict[str, Any]:
    """Quick TauBench mini-run — 3 trials max to keep latency reasonable."""
    out: Dict[str, Any] = {"available": False}
    try:
        from openjarvis.evals.datasets import taubench  # type: ignore
        out["available"] = True
        # Try the conventional entry points; record which exists
        candidates: List[str] = []
        for fn_name in ("run_suite", "run", "evaluate", "score"):
            if callable(getattr(taubench, fn_name, None)):
                candidates.append(fn_name)
        out["entry_points"] = candidates
        # Don't actually run it from the debug endpoint — too slow and may
        # need GPU/large models.  Just confirm the harness is reachable.
        out["note"] = "harness reachable; run separately via `python -m openjarvis.evals.runner`"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/v1/_debug/agentic")
async def debug_agentic(request: Request) -> Dict[str, Any]:
    """Run a synchronous probe of every agentic layer and return structured JSON."""
    started = time.time()
    app_state = request.app.state

    env_block = _probe_env()
    skills_block = _probe_skills(app_state)
    goal_block = _probe_goal_tracker()
    skill_planner_block = _probe_skill_planner()
    reflector_block = _probe_reflector(app_state)
    orchestrator_block = await _probe_orchestrator()
    distiller_block = _probe_distiller()
    evolver_block = _probe_prompt_evolver()
    eval_block = _probe_eval()

    return {
        "_timestamp": datetime.now(timezone.utc).isoformat(),
        "_elapsed_ms": int((time.time() - started) * 1000),
        "env": env_block,
        "skills": skills_block,
        "goal_tracker": goal_block,
        "skill_planner": skill_planner_block,
        "reflector": reflector_block,
        "orchestrator": orchestrator_block,
        "distiller": distiller_block,
        "prompt_evolver": evolver_block,
        "eval": eval_block,
    }
