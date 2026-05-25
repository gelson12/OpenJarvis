"""Skill-aware planner (Round 3.3 of OpenJarvis-native upgrade).

Before the LLM is invoked to decompose a multi-step query, the planner
searches the TOML skill library (`skills/manager.py`) for matching skills.

  - **Match found** → execute the matched skill chain directly. Faster +
    more reliable than LLM decomposition; the skill was authored or
    distilled specifically for this kind of task.
  - **No match** → fall through to the regular LLM-driven flow (the agent
    runs as it did before this layer existed).

This is what Hermes's planner structurally cannot do — Hermes has no skill
library to consult, so it always invokes the LLM.  OpenJarvis's mature
TOML skills + dependency graph + security checks provide a much faster path
when the user's request matches a known recipe.

Env gate: OPENJARVIS_SKILL_PLANNER_ENABLED (default false; opt-in after
verifying skill matches are accurate enough not to misfire).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_SKILL_PLANNER_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Keyword scoring against skill manifests
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "with", "for", "to",
    "of", "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "i",
    "me", "my", "you", "your", "he", "she", "it", "we", "us", "they", "them",
    "this", "that", "these", "those", "can", "could", "would", "should",
    "will", "shall", "may", "might", "must", "please",
})


def _tokenize(text: str) -> List[str]:
    return [
        t for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _STOP_WORDS and len(t) > 2
    ]


@dataclass
class SkillMatch:
    skill_name: str
    score: float
    manifest: Any  # SkillManifest from openjarvis.skills.parser


def find_matching_skill(query: str, *, threshold: float = 0.22) -> Optional[SkillMatch]:
    """Search the loaded TOML skill library for a skill whose name +
    description tokens overlap the query. Returns the best match above
    `threshold`, or None.

    Threshold tuned conservatively: we want zero false-positive skill
    invocations (a wrong skill is worse than just running the LLM).
    """
    if not _enabled() or not query or not query.strip():
        return None

    try:
        from openjarvis.skills.manager import SkillManager
    except Exception as exc:
        logger.debug("skill_planner: SkillManager import failed: %s", exc)
        return None

    try:
        # SkillManager requires an EventBus; create a throwaway one for the
        # planner's read-only discovery.  discover() with no paths loads
        # zero skills — we must point it at the bundled data dir.
        try:
            from openjarvis.core.events import EventBus
            _bus = EventBus()
        except Exception:
            _bus = None
        manager = SkillManager(_bus) if _bus is not None else None
        if manager is None:
            return None
        try:
            from pathlib import Path as _Path
            import openjarvis.skills as _skills_pkg
            _bundled = _Path(_skills_pkg.__file__).parent / "data"
            manager.discover(paths=[_bundled])
        except Exception:
            # SkillManager may already have been discovered at startup;
            # try to access its in-memory state directly.
            pass
        # SkillManager keeps manifests in self._skills (no public accessor).
        manifests: List[Any] = []
        _sd = getattr(manager, "_skills", None)
        if isinstance(_sd, dict):
            manifests = list(_sd.values())
    except Exception as exc:
        logger.debug("skill_planner: discover failed: %s", exc)
        return None

    if not manifests:
        return None

    qtokens = set(_tokenize(query))
    if not qtokens:
        return None

    best: Optional[SkillMatch] = None
    for m in manifests:
        name = ""
        description = ""
        for attr in ("name", "skill_name", "id"):
            v = getattr(m, attr, None)
            if isinstance(v, str) and v.strip():
                name = v
                break
        for attr in ("description", "summary", "doc"):
            v = getattr(m, attr, None)
            if isinstance(v, str) and v.strip():
                description = v
                break
        if not name:
            continue
        stokens = set(_tokenize(name)) | set(_tokenize(description))
        if not stokens:
            continue
        overlap = len(qtokens & stokens) / max(len(qtokens), 1)
        if overlap >= threshold and (best is None or overlap > best.score):
            best = SkillMatch(skill_name=name, score=overlap, manifest=m)

    return best


# ---------------------------------------------------------------------------
# Skill execution
# ---------------------------------------------------------------------------

def execute_skill_chain(match: SkillMatch, query: str,
                       *, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Execute the matched TOML skill via OpenJarvis's existing skill
    executor.  Returns the final string output, or None on any failure
    (caller falls through to LLM flow)."""
    if match is None or match.manifest is None:
        return None
    try:
        from openjarvis.skills.tool_adapter import SkillTool
    except Exception as exc:
        logger.debug("skill_planner: SkillTool import failed: %s", exc)
        return None
    try:
        tool = SkillTool(match.manifest)
    except Exception as exc:
        logger.debug("skill_planner: SkillTool construct failed: %s", exc)
        return None

    # The skill's argument template is usually parameterised by the user
    # query; pass the raw query plus any context we have.
    args: Dict[str, Any] = {"query": query}
    if context:
        args.update(context)
    try:
        # SkillTool exposes either `.execute()` or `.run()` depending on the
        # OpenJarvis version — try both.
        for method in ("execute", "run", "__call__"):
            fn = getattr(tool, method, None)
            if callable(fn):
                result = fn(**args)
                break
        else:
            return None
    except Exception as exc:
        logger.debug("skill_planner: execute failed: %s", exc)
        return None

    # Coerce to string for return to the chat layer.
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for k in ("output", "text", "content", "result", "answer"):
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return None


# ---------------------------------------------------------------------------
# Top-level entry — called from routes.py or agents/_stubs.py
# ---------------------------------------------------------------------------

def maybe_handle(query: str, *, threshold: float = 0.35,
                 context: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """If a known TOML skill matches the query above `threshold`, execute
    that skill chain and return its output. Otherwise return None so the
    caller falls through to the LLM-driven flow.

    Returns the skill's output string on a clean execution, None on any
    miss or failure (so the caller can fall through transparently)."""
    if not _enabled():
        return None
    match = find_matching_skill(query, threshold=threshold)
    if not match:
        return None
    logger.info(
        "openjarvis.skill_planner.match skill=%s score=%.2f",
        match.skill_name, match.score,
    )
    result = execute_skill_chain(match, query, context=context)
    if result is None:
        logger.debug("openjarvis.skill_planner.fallthrough skill=%s failed", match.skill_name)
        return None
    logger.info(
        "openjarvis.skill_planner.executed skill=%s output_len=%d",
        match.skill_name, len(result),
    )
    return result
