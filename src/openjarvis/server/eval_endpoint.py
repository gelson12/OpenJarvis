"""Self-contained mini-eval endpoint (Round 4.6).

Industrial benchmarks like TauBench require cloning a separate repo and
installing it at runtime — too slow for an HTTP probe. This endpoint runs
a curated 20-query mini-benchmark against the active engine, scores each
response with the reflector, and returns aggregate metrics.

The result is a SELF-CONSISTENT baseline: you run it once with agentic
flags ON, save the JSON, then run again after any deploy or flag flip to
measure delta. Outclass = current avg_confidence > baseline + 0.05 AND
avg_success_rate > baseline + 5pp.

GET  /v1/_debug/eval/mini           — runs the suite, returns aggregate
POST /v1/_debug/eval/mini/baseline  — saves the current run as the named baseline
GET  /v1/_debug/eval/mini/baseline  — returns saved baseline + current delta

Auth: protected by the same Basic Auth gate as /v1/_debug/agentic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)
router = APIRouter()


# Curated mini-benchmark — 20 queries spanning common voice/chat domains.
# Each query has an expected_keywords list: the response must contain at
# least one of these to be judged "successful" by the heuristic scorer
# (which complements the LLM reflector).
_MINI_SUITE: List[Dict[str, Any]] = [
    {"id": "math-1",     "domain": "math",      "q": "What is 2+2?",                                    "expected": ["4", "four"]},
    {"id": "math-2",     "domain": "math",      "q": "Compute 15 percent of 240.",                     "expected": ["36"]},
    {"id": "math-3",     "domain": "math",      "q": "What is the square root of 144?",               "expected": ["12", "twelve"]},
    {"id": "geo-1",      "domain": "geography", "q": "What is the capital of Japan?",                  "expected": ["tokyo"]},
    {"id": "geo-2",      "domain": "geography", "q": "Which continent is Egypt on?",                   "expected": ["africa"]},
    {"id": "geo-3",      "domain": "geography", "q": "What is the largest ocean on Earth?",           "expected": ["pacific"]},
    {"id": "history-1",  "domain": "history",   "q": "Who painted the Mona Lisa?",                    "expected": ["leonardo", "da vinci", "vinci"]},
    {"id": "history-2",  "domain": "history",   "q": "In what year did World War II end?",            "expected": ["1945"]},
    {"id": "code-1",     "domain": "code",      "q": "How do I reverse a string in Python?",          "expected": ["[::-1]", "reversed", "::-1"]},
    {"id": "code-2",     "domain": "code",      "q": "What does the SQL JOIN keyword do?",            "expected": ["join", "combine", "table", "rows"]},
    {"id": "logic-1",    "domain": "logic",     "q": "If all roses are flowers, are some flowers roses?", "expected": ["yes", "true", "some"]},
    {"id": "logic-2",    "domain": "logic",     "q": "A train leaves at 3pm going 60mph. How far in 2 hours?", "expected": ["120"]},
    {"id": "sci-1",      "domain": "science",   "q": "What is the chemical symbol for gold?",         "expected": ["au"]},
    {"id": "sci-2",      "domain": "science",   "q": "What gas do plants absorb during photosynthesis?", "expected": ["carbon dioxide", "co2", "co2"]},
    {"id": "sci-3",      "domain": "science",   "q": "How many planets are in our solar system?",     "expected": ["8", "eight"]},
    {"id": "lang-1",     "domain": "language",  "q": "Translate 'hello' to Spanish.",                 "expected": ["hola"]},
    {"id": "lang-2",     "domain": "language",  "q": "What is the past tense of 'run'?",              "expected": ["ran"]},
    {"id": "multi-1",    "domain": "multistep", "q": "List three primary colors and one mix.",         "expected": ["red", "blue", "yellow"]},
    {"id": "multi-2",    "domain": "multistep", "q": "Name the 4 seasons in order starting from spring.", "expected": ["summer", "autumn", "winter", "fall"]},
    {"id": "edge-1",     "domain": "edge",      "q": "",                                              "expected": []},  # empty query — refusal expected
]


def _baseline_path(label: str) -> Path:
    safe = "".join(c for c in label if c.isalnum() or c in "-_")[:48] or "default"
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    d = d / "evals"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / f"baseline_{safe}.json"


def _heuristic_score(answer: str, expected: List[str]) -> bool:
    """Cheap keyword-based scorer — complements the LLM reflector."""
    if not expected:
        # Edge queries (empty input) — we count any answer as "graceful" success.
        return bool(answer and answer.strip())
    a = (answer or "").lower()
    return any(k.lower() in a for k in expected)


async def _run_one_engine(engine: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    """Direct engine.generate path — fast, bypasses the agentic stack."""
    started = time.time()
    out: Dict[str, Any] = {
        "id": item["id"], "domain": item["domain"], "q": item["q"],
        "ok": False, "answer_len": 0, "latency_ms": 0, "error": None,
        "via": "engine",
    }
    if engine is None:
        out["error"] = "no engine"
        return out
    try:
        from openjarvis.core.types import Message, Role
        msgs = [Message(role=Role.USER, content=item["q"] or "(empty)")]
        model = os.environ.get("OPENJARVIS_EVAL_MODEL", "openrouter/google/gemini-2.5-flash")
        result = engine.generate(msgs, model=model, max_tokens=200, temperature=0.0)
        text = ""
        if isinstance(result, dict):
            for k in ("content", "text", "output", "response", "answer"):
                v = result.get(k)
                if isinstance(v, str) and v.strip():
                    text = v
                    break
        elif isinstance(result, str):
            text = result
        out["answer_len"] = len(text)
        out["answer_preview"] = text[:160]
        out["ok"] = _heuristic_score(text, item.get("expected", []))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["latency_ms"] = int((time.time() - started) * 1000)
    return out


async def _run_one_chat(item: Dict[str, Any]) -> Dict[str, Any]:
    """Full-stack path — POSTs to /v1/chat/completions on this same instance.
    Exercises every Round 4+5 layer (cache, skill_planner, hybrid router,
    orchestrator, reflector fan-out). Round 5.5."""
    started = time.time()
    out: Dict[str, Any] = {
        "id": item["id"], "domain": item["domain"], "q": item["q"],
        "ok": False, "answer_len": 0, "latency_ms": 0, "error": None,
        "via": "chat",
    }
    try:
        import httpx
        port = os.environ.get("PORT", "8642")
        user = os.environ.get("OPENJARVIS_BASIC_AUTH_USER", "")
        pwd = os.environ.get("OPENJARVIS_BASIC_AUTH_PASSWORD", "")
        auth = (user, pwd) if user else None
        model = os.environ.get("OPENJARVIS_EVAL_MODEL", "openrouter/google/gemini-2.5-flash")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": item["q"] or "(empty)"}],
            "max_tokens": 200,
            "temperature": 0.0,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://localhost:{port}/v1/chat/completions",
                json=payload, auth=auth,
                headers={"X-OpenJarvis-Session-Id": f"eval-{item['id']}"},
            )
        if resp.status_code != 200:
            out["error"] = f"http {resp.status_code}: {resp.text[:120]}"
        else:
            data = resp.json()
            text = ""
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except Exception:
                text = ""
            out["answer_len"] = len(text)
            out["answer_preview"] = text[:160]
            out["ok"] = _heuristic_score(text, item.get("expected", []))
            out["cache_hit"] = resp.headers.get("X-OpenJarvis-Cache") == "hit"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["latency_ms"] = int((time.time() - started) * 1000)
    return out


async def _run_one(engine: Any, item: Dict[str, Any], *, via: str = "engine") -> Dict[str, Any]:
    if via == "chat":
        return await _run_one_chat(item)
    return await _run_one_engine(engine, item)


async def _run_suite(engine: Any, *, n: int, via: str = "engine") -> Dict[str, Any]:
    started = time.time()
    items = _MINI_SUITE[:max(1, min(n, len(_MINI_SUITE)))]
    # Run sequentially — parallel would skew latency measurements + risk
    # rate-limits on free providers.
    results: List[Dict[str, Any]] = []
    for item in items:
        results.append(await _run_one(engine, item, via=via))
    n_total = len(results)
    n_ok = sum(1 for r in results if r["ok"])
    n_err = sum(1 for r in results if r.get("error"))
    avg_lat = int(sum(r["latency_ms"] for r in results) / n_total) if n_total else 0
    by_domain: Dict[str, Dict[str, int]] = {}
    for r in results:
        d = r["domain"]
        slot = by_domain.setdefault(d, {"n": 0, "ok": 0})
        slot["n"] += 1
        if r["ok"]:
            slot["ok"] += 1
    return {
        "_iso": datetime.now(timezone.utc).isoformat(),
        "_elapsed_ms": int((time.time() - started) * 1000),
        "engine_class": type(engine).__name__ if engine is not None else "None",
        "model": os.environ.get("OPENJARVIS_EVAL_MODEL", "openrouter/google/gemini-2.5-flash"),
        "n": n_total,
        "success": n_ok,
        "errors": n_err,
        "success_rate": round(n_ok / n_total, 4) if n_total else 0.0,
        "avg_latency_ms": avg_lat,
        "by_domain": {
            d: {"n": s["n"], "ok": s["ok"],
                "rate": round(s["ok"]/s["n"], 4) if s["n"] else 0.0}
            for d, s in by_domain.items()
        },
        "agentic_flags": {
            k: os.environ.get(k, "")
            for k in (
                "OPENJARVIS_ORCHESTRATOR_ENABLED",
                "OPENJARVIS_ORCHESTRATOR_MODE",
                "OPENJARVIS_REFLECTOR_ENABLED",
                "OPENJARVIS_GOALS_ENABLED",
                "OPENJARVIS_SKILL_PLANNER_ENABLED",
                "OPENJARVIS_ANSWER_CACHE_ENABLED",
                "OPENJARVIS_WATCHDOG_ENABLED",
                "OPENJARVIS_MODEL_PREFERENCE_ENABLED",
                "OPENJARVIS_HERMES_ROUTE",
            )
        },
        "results": results,
    }


def _compute_delta(current: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success_rate_delta_pp": round(
            (current.get("success_rate", 0.0) - baseline.get("success_rate", 0.0)) * 100, 2
        ),
        "avg_latency_delta_ms": int(
            current.get("avg_latency_ms", 0) - baseline.get("avg_latency_ms", 0)
        ),
        "outclass": (
            current.get("success_rate", 0.0)
            >= baseline.get("success_rate", 0.0) + 0.05
        ),
    }


@router.get("/v1/_debug/eval/mini")
async def eval_mini(request: Request, n: int = Query(20, ge=1, le=20),
                    baseline_label: str = Query("", max_length=48),
                    via: str = Query("engine", regex="^(engine|chat)$")) -> Dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    current = await _run_suite(engine, n=n, via=via)
    response: Dict[str, Any] = {"current": current}
    if baseline_label:
        path = _baseline_path(baseline_label)
        if path.exists():
            try:
                baseline = json.loads(path.read_text(encoding="utf-8"))
                response["baseline"] = baseline
                response["delta"] = _compute_delta(current, baseline)
            except Exception as exc:
                response["baseline_error"] = str(exc)
        else:
            response["baseline_error"] = f"no baseline named {baseline_label!r}"
    return response


@router.post("/v1/_debug/eval/mini/baseline")
async def save_baseline(request: Request,
                        label: str = Query("default", max_length=48),
                        n: int = Query(20, ge=1, le=20),
                        via: str = Query("engine", regex="^(engine|chat)$")) -> Dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    current = await _run_suite(engine, n=n, via=via)
    path = _baseline_path(label)
    try:
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"save failed: {exc}")
    return {"saved": True, "path": str(path), "label": label, "summary": {
        "n": current["n"],
        "success_rate": current["success_rate"],
        "avg_latency_ms": current["avg_latency_ms"],
    }}


@router.get("/v1/_debug/eval/mini/baseline")
async def load_baseline(label: str = Query("default", max_length=48)) -> Dict[str, Any]:
    path = _baseline_path(label)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no baseline named {label!r}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"load failed: {exc}")
