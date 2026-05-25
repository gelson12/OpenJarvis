"""Post-turn reflection (Round 2.1 of OpenJarvis-native agentic upgrade).

After each chat completion, an auxiliary LLM critique runs in the background
and emits a structured score {confidence, success, learning, refusal_risk,
follow_up_needed, tags}. The score is appended to
``~/.openjarvis/reflections/<session>.jsonl`` AND cached in-memory by
session id so downstream consumers (distillation trigger, prompt evolver,
goal-progress detection) can read it without disk I/O on the hot path.

Hermes's reflector uses a dedicated ``get_text_auxiliary_client`` abstraction;
OpenJarvis doesn't have one, so this module calls ``engine.generate(...)``
directly with a low-cost model preference (Gemini Flash, with fallback to
whatever the active engine provides).

Best-effort throughout: any failure (network, parse, no engine) is logged at
debug and the user-facing chat turn is unaffected. The reflection happens
*after* the response has already been delivered.

Env gates:
    OPENJARVIS_REFLECTOR_ENABLED       default false (opt-in until verified)
    OPENJARVIS_REFLECTOR_MODEL         optional override for the critic model
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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_REFLECT_SYSTEM = (
    "You are OpenJarvis's post-turn reflector. You read a (question, answer) pair "
    "and return ONLY a single JSON object with this shape:\n"
    "{\n"
    '  "confidence": <number 0..1>,\n'
    '  "success": <true|false>,\n'
    '  "learning": "<one short sentence — what this turn teaches OpenJarvis>",\n'
    '  "refusal_risk": <true|false>,\n'
    '  "follow_up_needed": <true|false>,\n'
    '  "tags": ["<short lowercase-kebab>", ...]    // 1-3 tags\n'
    "}\n"
    "Return JSON only. No prose. No markdown fences."
)


def _build_user_prompt(user_text: str, assistant_text: str,
                      domain: str = "", complexity: Optional[float] = None) -> str:
    user_trim = (user_text or "").strip()[:1200]
    asst_trim = (assistant_text or "").strip()[:2400]
    header = f"Domain: {domain or 'general'}"
    if complexity is not None:
        header += f" | Complexity: {complexity:.2f}"
    return (
        f"{header}\n\n"
        f"USER QUESTION:\n{user_trim}\n\n"
        f"ASSISTANT ANSWER:\n{asst_trim}\n\n"
        "Return JSON only."
    )


# ---------------------------------------------------------------------------
# JSON extraction (auxiliary models sometimes wrap in fences)
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                     flags=re.IGNORECASE | re.MULTILINE)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    m = _JSON_BLOCK_RE.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _normalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
    def _bool(v, default=False) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "y")
        return default

    def _num(v, default=0.5) -> float:
        try:
            f = float(v)
        except Exception:
            return default
        return max(0.0, min(1.0, f))

    raw_tags = parsed.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = [str(raw_tags)]
    tags = []
    for t in raw_tags[:3]:
        if not isinstance(t, str):
            continue
        slug = re.sub(r"[^a-z0-9-]+", "-", t.lower().strip()).strip("-")
        if slug:
            tags.append(slug[:40])

    return {
        "confidence": _num(parsed.get("confidence"), 0.5),
        "success": _bool(parsed.get("success"), False),
        "learning": str(parsed.get("learning") or "").strip()[:280],
        "refusal_risk": _bool(parsed.get("refusal_risk"), False),
        "follow_up_needed": _bool(parsed.get("follow_up_needed"), False),
        "tags": tags,
    }


# ---------------------------------------------------------------------------
# Engine call — uses OpenJarvis's engine.generate directly
# ---------------------------------------------------------------------------

# Engine instance reference — populated by app.py startup so we have
# access to the real InferenceEngine on app.state.engine without needing
# the request context.  Without this, the module-level _ENGINE is None
# and reflection silently no-ops (logged at debug).
_ENGINE: Any = None


def set_engine(engine: Any) -> None:
    """Called from server/app.py startup so the reflector can call the
    real InferenceEngine instance (which lives on app.state.engine and
    isn't reachable via a module-level import)."""
    global _ENGINE
    _ENGINE = engine


def _call_engine(system: str, user: str, *, max_tokens: int = 400,
                 temperature: float = 0.2, engine: Any = None) -> Optional[str]:
    """Best-effort engine call. Returns text or None.

    `engine` overrides the module-level _ENGINE (set by app.py startup).
    The debug endpoint passes request.app.state.engine directly so the
    probe works even if set_engine hasn't fired yet.
    """
    model_override = os.environ.get("OPENJARVIS_REFLECTOR_MODEL", "").strip() or None
    eng = engine if engine is not None else _ENGINE
    if eng is None:
        logger.debug("reflector: no engine available (set_engine not called yet)")
        return None

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kwargs: Dict[str, Any] = {"max_tokens": max_tokens, "temperature": temperature}
    if model_override:
        kwargs["model"] = model_override

    try:
        result = eng.generate(messages, **kwargs)
    except Exception as exc:
        logger.debug("reflector: engine.generate failed: %s", exc)
        return None

    # engine.generate returns a dict with "content" key per OpenJarvis
    # InferenceEngine contract (see engine/_stubs.py).
    if isinstance(result, dict):
        for k in ("content", "text", "output", "response", "answer"):
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    if isinstance(result, str):
        return result.strip() or None
    # Some engines return an object with .choices[0].message.content
    try:
        return result.choices[0].message.content
    except Exception:
        return None


# ---------------------------------------------------------------------------
# In-memory cache + disk persistence
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_RECENT: Dict[str, Dict[str, Any]] = {}
_MAX_TRACKED = 256


def _reflections_dir() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    if base:
        d = Path(base) / "reflections"
    else:
        d = Path.home() / ".openjarvis" / "reflections"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def last_reflection(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the most recent reflection for a session (consumed by distiller
    online-trigger gate AND prompt evolver promotion gate).
    """
    if not session_id:
        return None
    with _LOCK:
        return _RECENT.get(session_id)


def _store_recent(session_id: str, reflection: Dict[str, Any]) -> None:
    if not session_id:
        return
    with _LOCK:
        _RECENT[session_id] = reflection
        if len(_RECENT) > _MAX_TRACKED:
            victims = sorted(_RECENT.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in victims[: _MAX_TRACKED // 4]:
                _RECENT.pop(k, None)


def _append_jsonl(session_id: str, reflection: Dict[str, Any]) -> None:
    try:
        path = _reflections_dir() / f"{re.sub(r'[^a-zA-Z0-9_.-]', '_', session_id)[:64]}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(reflection, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("reflector: jsonl write failed: %s", exc)


# ---------------------------------------------------------------------------
# Public entry — fire-and-forget
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_REFLECTOR_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def _do_reflect(session_id: str, user_text: str, assistant_text: str,
                domain: str, complexity: Optional[float]) -> None:
    user_prompt = _build_user_prompt(user_text, assistant_text, domain=domain, complexity=complexity)
    raw = _call_engine(_REFLECT_SYSTEM, user_prompt)
    if not raw:
        logger.debug("reflector: no engine output for session=%s", session_id)
        return

    parsed = _extract_json(raw)
    if not parsed:
        logger.debug("reflector: could not parse JSON; first 120c: %r", raw[:120])
        return

    norm = _normalize(parsed)
    norm.update({
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "domain": domain or "general",
    })
    if complexity is not None:
        norm["complexity"] = complexity

    _store_recent(session_id, norm)
    _append_jsonl(session_id, norm)

    logger.info(
        "openjarvis.reflector.score session=%s conf=%.2f success=%s refusal=%s followup=%s tags=%s",
        session_id, norm["confidence"], norm["success"], norm["refusal_risk"],
        norm["follow_up_needed"], norm["tags"],
    )


def reflect_async(*, session_id: str, user_text: str, assistant_text: str,
                  domain: str = "general", complexity: Optional[float] = None) -> None:
    """Spawn a background thread for post-turn reflection. Best-effort —
    failure doesn't affect the live chat response."""
    if not _enabled():
        return
    if not user_text or not assistant_text:
        return
    thread = threading.Thread(
        target=_do_reflect,
        args=(session_id, user_text, assistant_text, domain, complexity),
        name="openjarvis-reflector",
        daemon=True,
    )
    thread.start()
