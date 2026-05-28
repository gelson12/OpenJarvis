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
    "OPENJARVIS_HERMES_ROUTE",
    # Round 4 layers
    "OPENJARVIS_ANSWER_CACHE_ENABLED",
    "OPENJARVIS_WATCHDOG_ENABLED",
    "OPENJARVIS_WATCHDOG_AUTOROLLBACK",
    "OPENJARVIS_MODEL_PREFERENCE_ENABLED",
    "OPENJARVIS_COST_TELEMETRY_ENABLED",
    # Round 5 closed-loop + bonus layers
    "OPENJARVIS_ANSWER_CACHE_LOOKUP_ENABLED",
    "OPENJARVIS_MODEL_PREFERENCE_TIEBREAK_ENABLED",
    "OPENJARVIS_HYBRID_ROUTING_ENABLED",
    "OPENJARVIS_HYBRID_COMPLEXITY_CONSENSUS",
    "OPENJARVIS_ONLINE_DISTILLER_ENABLED",
    "OPENJARVIS_SPECULATIVE_RACE_ENABLED",
    "OPENJARVIS_TEMP_TUNER_ENABLED",
    "OPENJARVIS_SKILL_PROPOSER_ENABLED",
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
                from openjarvis.core.events import EventBus
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
        # SkillManager stores manifests in self._skills (no public accessor).
        _skills_dict = getattr(mgr, "_skills", None)
        if isinstance(_skills_dict, dict):
            manifests = list(_skills_dict.values())
        else:
            manifests = []
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
        out["engine_class"] = type(engine).__name__ if engine is not None else "None"
        # ALSO directly test engine.generate without the reflector wrapper
        # so we can see exactly what InstrumentedEngine returns.
        try:
            from openjarvis.core.types import Message, Role
            _probe_msgs = [Message(role=Role.USER, content="Say 'pong' and nothing else.")]
            raw_result = engine.generate(
                _probe_msgs,
                model="openrouter/auto",
                max_tokens=10,
                temperature=0,
            ) if engine is not None else None
            out["direct_engine_test"] = {
                "type": type(raw_result).__name__,
                "keys": list(raw_result.keys()) if isinstance(raw_result, dict) else None,
                "preview": str(raw_result)[:200],
            }
        except Exception as exc:
            out["direct_engine_test"] = {"error": f"{type(exc).__name__}: {exc}"}
        # ALSO call engine.generate directly with the reflector's exact
        # prompt + model, so we can see the raw response separately from
        # the reflector's JSON-parsing logic.
        try:
            from openjarvis.core.types import Message, Role
            _refl_msgs = [
                Message(role=Role.SYSTEM, content=_ref._REFLECT_SYSTEM),
                Message(role=Role.USER, content=_ref._build_user_prompt(
                    "What is 2+2?", "2+2 equals 4.", domain="debug-probe",
                )),
            ]
            _refl_result = engine.generate(
                _refl_msgs, model="openrouter/auto",
                max_tokens=400, temperature=0.2,
            ) if engine is not None else None
            if isinstance(_refl_result, dict):
                out["reflector_raw_engine"] = {
                    "content_preview": str(_refl_result.get("content"))[:240],
                    "model": _refl_result.get("model"),
                    "usage": _refl_result.get("usage"),
                }
        except Exception as exc:
            out["reflector_raw_engine"] = {"error": f"{type(exc).__name__}: {exc}"}

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


def _probe_answer_cache() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False}
    try:
        from openjarvis.learning import answer_cache as _ac
        return _ac.stats()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_watchdog() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False}
    try:
        from openjarvis.learning import watchdog as _wd
        return _wd.health_snapshot()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_model_preference() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False}
    try:
        from openjarvis.learning import model_preference as _mp
        return _mp.snapshot()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_cost_telemetry() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": False}
    try:
        from openjarvis.learning import cost_telemetry as _ct
        return _ct.snapshot()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_online_distiller() -> Dict[str, Any]:
    try:
        from openjarvis.learning import online_distiller as _od
        return _od.stats()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _probe_hybrid_router() -> Dict[str, Any]:
    try:
        from openjarvis.learning.routing import hybrid as _h
        return _h.snapshot()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _probe_speculative() -> Dict[str, Any]:
    try:
        from openjarvis.learning import speculative as _s
        return _s.snapshot()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _probe_temp_tuner() -> Dict[str, Any]:
    try:
        from openjarvis.learning import temperature_tuner as _t
        return _t.snapshot()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _probe_skill_proposer() -> Dict[str, Any]:
    try:
        from openjarvis.learning import skill_proposer as _sp
        return _sp.snapshot()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _probe_domain_classifier() -> Dict[str, Any]:
    try:
        from openjarvis.learning.domain_classifier import infer_domain, all_known_domains
        samples = {
            "What is 2+2?": infer_domain("What is 2+2?"),
            "How do I reverse a string in Python?": infer_domain("How do I reverse a string in Python?"),
            "Translate hello to Spanish": infer_domain("Translate hello to Spanish"),
        }
        return {"enabled": True, "domains": all_known_domains(), "samples": samples}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


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
# Round 9.7 — self-improvement loop probes
# ---------------------------------------------------------------------------

def _probe_learned_intents() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from openjarvis.server import learned_intents as _li
        out.update(_li.snapshot())
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_learned_prompt_hints() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from openjarvis.server import learned_prompt_hints as _ph
        out.update(_ph.snapshot())
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_disavowal_detector() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from openjarvis.server import disavowal_detector as _dd
        out.update(_dd.snapshot())
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _probe_learning_mirrors() -> Dict[str, Any]:
    """Combined block for the two durable mirrors (Postgres + Obsidian Mind)."""
    out: Dict[str, Any] = {"postgres": {}, "obsidian_mind": {}}
    try:
        from openjarvis.server import learning_pg as _pg
        out["postgres"] = _pg.snapshot()
    except Exception as e:
        out["postgres"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from openjarvis.server import learning_mind as _mind
        out["obsidian_mind"] = _mind.snapshot()
    except Exception as e:
        out["obsidian_mind"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _probe_tool_router() -> Dict[str, Any]:
    """Round 20 Piece 1 — embedding-based tool retrieval status."""
    try:
        from openjarvis.server import tool_router as _tr
        return _tr.snapshot()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


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
    answer_cache_block = _probe_answer_cache()
    watchdog_block = _probe_watchdog()
    model_pref_block = _probe_model_preference()
    cost_block = _probe_cost_telemetry()
    online_distiller_block = _probe_online_distiller()
    hybrid_block = _probe_hybrid_router()
    speculative_block = _probe_speculative()
    temp_tuner_block = _probe_temp_tuner()
    skill_proposer_block = _probe_skill_proposer()
    domain_classifier_block = _probe_domain_classifier()
    eval_block = _probe_eval()
    # Round 9.7 — universal self-improvement loop visibility
    learned_intents_block = _probe_learned_intents()
    learned_prompt_hints_block = _probe_learned_prompt_hints()
    disavowal_detector_block = _probe_disavowal_detector()
    learning_mirrors_block = _probe_learning_mirrors()
    # Round 20 Piece 1 — embedding-based tool router
    tool_router_block = _probe_tool_router()

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
        "answer_cache": answer_cache_block,
        "watchdog": watchdog_block,
        "model_preference": model_pref_block,
        "cost_telemetry": cost_block,
        # Round 5 closed-loop + bonus layers
        "online_distiller": online_distiller_block,
        "hybrid_router": hybrid_block,
        "speculative": speculative_block,
        "temperature_tuner": temp_tuner_block,
        "skill_proposer": skill_proposer_block,
        "domain_classifier": domain_classifier_block,
        "eval": eval_block,
        # Round 8 + 9 — self-improvement loop
        "disavowal_detector": disavowal_detector_block,
        "learned_intents": learned_intents_block,
        "learned_prompt_hints": learned_prompt_hints_block,
        "learning_mirrors": learning_mirrors_block,
        # Round 20 — autonomous architecture
        "tool_router": tool_router_block,
    }


# ---------------------------------------------------------------------------
# Round 5.10 — manual safe-restore for watchdog autorollback
# ---------------------------------------------------------------------------

@router.post("/v1/_debug/watchdog/restore")
async def watchdog_restore() -> Dict[str, Any]:
    """Re-enable the most recently autorolled-back flag. Manual rescue when
    autorollback misfires under transient bad data."""
    try:
        from openjarvis.learning import watchdog as _wd
        rollbacks = list(_wd._ROLLBACKS)
        if not rollbacks:
            return {"restored": False, "reason": "no rollback history"}
        last = rollbacks[-1]
        flag = last.get("flag")
        prev_value = last.get("from")
        if not flag or prev_value is None:
            return {"restored": False, "reason": "rollback record missing flag/from"}
        os.environ[flag] = prev_value
        return {"restored": True, "flag": flag, "value": prev_value, "from_rollback": last}
    except Exception as e:
        return {"restored": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Round 5.12 — real-traffic verification probe
# ---------------------------------------------------------------------------

@router.get("/v1/_debug/agentic/real_traffic")
async def real_traffic(request: Request) -> Dict[str, Any]:
    """Self-test: POST a canned query through /v1/chat/completions and check
    that reflector + cost_telemetry actually fired. Proves HERMES_ROUTE=off
    doesn't silently break the agentic fan-out."""
    import asyncio as _asyncio
    out: Dict[str, Any] = {"chat_ok": False, "reflector_fired": False,
                           "cost_recorded": False, "latency_ms": 0,
                           "all_green": False}
    started = time.time()
    session_id = f"real-traffic-{int(started)}"
    canned_query = "What is the capital of France?"
    try:
        import httpx
        port = os.environ.get("PORT", "8642")
        user = os.environ.get("OPENJARVIS_BASIC_AUTH_USER", "")
        pwd = os.environ.get("OPENJARVIS_BASIC_AUTH_PASSWORD", "")
        auth = (user, pwd) if user else None
        # Baseline cost call count before request
        try:
            from openjarvis.learning import cost_telemetry as _ct
            cost_before = (_ct.snapshot() or {}).get("total_calls", 0)
        except Exception:
            cost_before = -1
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://localhost:{port}/v1/chat/completions",
                json={"model": os.environ.get("OPENJARVIS_EVAL_MODEL", "openrouter/google/gemini-2.5-flash"),
                      "messages": [{"role": "user", "content": canned_query}],
                      "max_tokens": 80, "temperature": 0.0, "stream": False},
                auth=auth,
                headers={"X-OpenJarvis-Session-Id": session_id},
            )
        out["chat_status"] = resp.status_code
        out["chat_ok"] = (resp.status_code == 200)
        if out["chat_ok"]:
            try:
                out["chat_answer_preview"] = (resp.json()["choices"][0]["message"]["content"] or "")[:120]
            except Exception:
                out["chat_answer_preview"] = "<parse-failed>"
        # Reflector fires in a background thread; wait a bit
        await _asyncio.sleep(5.0)
        try:
            from openjarvis.learning import reflector as _ref
            ref = _ref.last_reflection(session_id)
            if ref:
                out["reflector_fired"] = True
                out["reflection"] = {
                    "confidence": ref.get("confidence"),
                    "success": ref.get("success"),
                    "domain": ref.get("domain"),
                    "tags": ref.get("tags"),
                }
        except Exception as e:
            out["reflector_error"] = f"{type(e).__name__}: {e}"
        try:
            from openjarvis.learning import cost_telemetry as _ct2
            cost_after = (_ct2.snapshot() or {}).get("total_calls", 0)
            out["cost_recorded"] = cost_before >= 0 and cost_after > cost_before
            out["cost_delta"] = cost_after - cost_before if cost_before >= 0 else None
        except Exception:
            pass
        out["all_green"] = bool(out["chat_ok"] and out["reflector_fired"] and out["cost_recorded"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["latency_ms"] = int((time.time() - started) * 1000)
    out["session_id"] = session_id
    return out
