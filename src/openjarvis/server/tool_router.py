"""Round 20 Piece 1 — Embedding-based tool retrieval.

The LLM has access to ~140 registered tools but the existing
``_select_relevant_tools`` filter uses hardcoded keyword groups
("gmail" → gmail_*, "calendar" → outlook_list_events, …). Any novel
phrasing that doesn't hit a trigger keyword leaves the LLM with only
the always-on set + its internal tools, and the LLM disavows the
capability rather than discovering the tool it actually has.

This module ranks every registered tool by semantic similarity to the
current user query and exposes the top-K. Two consumers:

* ``rank_tools_for_query(text, k)`` — used by the chat handler to
  augment the auto-inject list with a "you have these tools for this
  query" system message.
* ``introspect_tools`` (Piece 2) — meta-tool the LLM can call to ask
  the router directly when uncertain about its own capabilities.

Design choices
--------------
* Embed each tool's ``name + description + parameter names`` once at
  first use and cache to ``~/.openjarvis/tool_embeddings.npz``. The
  cache key is a hash of the sorted tool-name set so we automatically
  re-embed when the registry changes.
* Lazy embedder init: if ``sentence-transformers`` is unavailable
  (test env, optional dep) we degrade gracefully — every public
  function returns empty results and never raises.
* Best-effort throughout. The chat path must keep working even when
  the router fails. The existing keyword-based filter remains the
  primary signal; the router augments it.

Env flags
~~~~~~~~~
``OPENJARVIS_TOOL_ROUTER_ENABLED`` (default ``true``)
    Master switch for the router.
``OPENJARVIS_TOOL_ROUTER_TOP_K`` (default ``8``)
    How many tools to return from ``rank_tools_for_query``.
``OPENJARVIS_TOOL_ROUTER_MIN_SCORE`` (default ``0.15``)
    Minimum cosine similarity to include in the result. Below this we
    treat the tool as irrelevant rather than padding with noise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_TOOL_ROUTER_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _top_k_default() -> int:
    try:
        return max(1, min(50, int(os.environ.get("OPENJARVIS_TOOL_ROUTER_TOP_K", "8"))))
    except Exception:
        return 8


def _min_score() -> float:
    try:
        v = float(os.environ.get("OPENJARVIS_TOOL_ROUTER_MIN_SCORE", "0.15"))
        return max(0.0, min(1.0, v))
    except Exception:
        return 0.15


def _home() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _cache_path() -> Path:
    return _home() / "tool_embeddings.npz"


# ---------------------------------------------------------------------------
# Embedder singleton (lazy)
# ---------------------------------------------------------------------------


_EMBEDDER_LOCK = threading.Lock()
_EMBEDDER: Optional[Any] = None
_EMBEDDER_FAILED: bool = False


def _get_embedder() -> Optional[Any]:
    """Return a process-wide singleton ``Embedder``, or None if unavailable.

    Lazy: only imported and initialised on first call. If the import
    fails (no ``sentence-transformers`` in the env) we cache the
    failure and never retry — the router silently degrades.
    """
    global _EMBEDDER, _EMBEDDER_FAILED
    if _EMBEDDER is not None:
        return _EMBEDDER
    if _EMBEDDER_FAILED:
        return None
    with _EMBEDDER_LOCK:
        if _EMBEDDER is not None:
            return _EMBEDDER
        if _EMBEDDER_FAILED:
            return None
        try:
            from openjarvis.tools.storage.embeddings import (
                SentenceTransformerEmbedder,
            )
            _EMBEDDER = SentenceTransformerEmbedder()
            logger.info(
                "tool_router: SentenceTransformerEmbedder ready (dim=%d)",
                _EMBEDDER.dim(),
            )
            return _EMBEDDER
        except Exception as exc:
            _EMBEDDER_FAILED = True
            logger.warning(
                "tool_router: embedder unavailable (%s) — router disabled", exc,
            )
            return None


# ---------------------------------------------------------------------------
# Tool corpus + embedding cache
# ---------------------------------------------------------------------------


_CORPUS_LOCK = threading.Lock()
_TOOL_NAMES: List[str] = []
_TOOL_DESCRIPTIONS: List[str] = []
_TOOL_EMBEDDINGS: Optional[Any] = None  # numpy.ndarray, shape (N, dim)
_REGISTRY_HASH: Optional[str] = None


def _enumerate_tools() -> List[Tuple[str, str]]:
    """Walk ``ToolRegistry`` and produce ``(name, description_text)`` pairs.

    Description text combines the tool's name + spec.description +
    parameter names so the embedding captures both what the tool does
    and what arguments it accepts.
    """
    try:
        from openjarvis.core.registry import ToolRegistry
    except Exception as exc:
        logger.debug("tool_router: ToolRegistry import failed: %s", exc)
        return []
    out: List[Tuple[str, str]] = []
    for name, cls in ToolRegistry.items():
        try:
            inst = cls()
            spec = inst.spec
            desc = (spec.description or "").strip()
            props = (spec.parameters or {}).get("properties", {}) or {}
            param_names = " ".join(props.keys()) if props else ""
            cat = (spec.category or "").strip()
            corpus = f"{name}. {desc}"
            if cat:
                corpus += f" Category: {cat}."
            if param_names:
                corpus += f" Parameters: {param_names}."
            out.append((name, corpus))
        except Exception as exc:
            logger.debug("tool_router: skip tool %s (%s)", name, exc)
            continue
    return out


def _registry_hash(names: List[str]) -> str:
    blob = "\n".join(sorted(names)).encode("utf-8", "replace")
    return hashlib.sha1(blob).hexdigest()[:16]


def _load_cache(expected_hash: str, dim: int) -> Optional[Tuple[List[str], List[str], Any]]:
    """Load (names, descriptions, embeddings) if the cache file matches
    ``expected_hash`` AND ``dim``. Returns None on any mismatch / error."""
    try:
        import numpy as np  # noqa: F401
    except Exception:
        return None
    p = _cache_path()
    if not p.exists():
        return None
    try:
        import numpy as np
        data = np.load(p, allow_pickle=True)
        cached_hash = str(data.get("hash", ""))
        cached_dim = int(data.get("dim", 0)) if "dim" in data.files else 0
        if cached_hash != expected_hash or cached_dim != dim:
            return None
        names = list(data["names"])
        descs = list(data["descriptions"])
        embeds = data["embeddings"]
        return names, descs, embeds
    except Exception as exc:
        logger.debug("tool_router: cache load failed: %s", exc)
        return None


def _save_cache(names: List[str], descs: List[str], embeds: Any, h: str, dim: int) -> None:
    try:
        import numpy as np
        np.savez(
            _cache_path(),
            names=np.array(names, dtype=object),
            descriptions=np.array(descs, dtype=object),
            embeddings=embeds,
            hash=h,
            dim=dim,
        )
    except Exception as exc:
        logger.debug("tool_router: cache save failed: %s", exc)


def _ensure_corpus() -> bool:
    """Populate the module-level corpus + embeddings if not yet built.

    Returns True when the corpus is ready for query, False on any
    failure (embedder missing, registry empty, embed failed). The
    chat path tolerates False — it just falls back to the existing
    keyword filter.
    """
    global _TOOL_NAMES, _TOOL_DESCRIPTIONS, _TOOL_EMBEDDINGS, _REGISTRY_HASH
    if _TOOL_EMBEDDINGS is not None:
        # Cheap check: did the registry change since last build?
        current = _registry_hash([n for n, _ in _enumerate_tools()])
        if current == _REGISTRY_HASH:
            return True
        # Registry changed — rebuild.
        logger.info("tool_router: registry hash changed, rebuilding corpus")
    embedder = _get_embedder()
    if embedder is None:
        return False
    with _CORPUS_LOCK:
        # Re-check inside the lock — another thread may have built it.
        if _TOOL_EMBEDDINGS is not None and _REGISTRY_HASH is not None:
            current = _registry_hash([n for n, _ in _enumerate_tools()])
            if current == _REGISTRY_HASH:
                return True
        pairs = _enumerate_tools()
        if not pairs:
            return False
        names = [n for n, _ in pairs]
        descs = [d for _, d in pairs]
        h = _registry_hash(names)
        dim = embedder.dim()
        # Try cache first
        cached = _load_cache(h, dim)
        if cached is not None:
            _TOOL_NAMES, _TOOL_DESCRIPTIONS, _TOOL_EMBEDDINGS = cached
            _REGISTRY_HASH = h
            logger.info(
                "tool_router: loaded %d tool embeddings from cache",
                len(_TOOL_NAMES),
            )
            return True
        # Embed from scratch
        try:
            t0 = time.time()
            embeds = embedder.embed(descs)
            elapsed_ms = int((time.time() - t0) * 1000)
            _TOOL_NAMES = names
            _TOOL_DESCRIPTIONS = descs
            _TOOL_EMBEDDINGS = embeds
            _REGISTRY_HASH = h
            _save_cache(names, descs, embeds, h, dim)
            logger.warning(
                "tool_router: embedded %d tools in %dms (dim=%d) — cached to %s",
                len(names), elapsed_ms, dim, _cache_path(),
            )
            return True
        except Exception as exc:
            logger.warning("tool_router: embed batch failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rank_tools_for_query(
    query: str, *, top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return the top-K tools most relevant to ``query``.

    Each result: ``{name, description, score}`` where score is cosine
    similarity in [0, 1]. Sorted by score descending. Tools below
    ``OPENJARVIS_TOOL_ROUTER_MIN_SCORE`` are filtered out.

    Best-effort: returns an empty list when disabled / embedder
    unavailable / registry empty. Never raises.
    """
    if not _enabled() or not query:
        return []
    if not _ensure_corpus():
        return []
    embedder = _get_embedder()
    if embedder is None or _TOOL_EMBEDDINGS is None:
        return []
    try:
        import numpy as np
        q = embedder.embed([query])
        # Normalise so dot product = cosine
        def _norm(a: Any) -> Any:
            n = np.linalg.norm(a, axis=1, keepdims=True)
            n = np.where(n == 0, 1.0, n)
            return a / n
        q_n = _norm(q)
        t_n = _norm(_TOOL_EMBEDDINGS)
        scores = (t_n @ q_n.T).flatten()  # (N,)
        k = top_k if top_k is not None else _top_k_default()
        k = max(1, min(k, len(_TOOL_NAMES)))
        # Get top-K indices
        top_idx = np.argsort(-scores)[:k]
        min_s = _min_score()
        out: List[Dict[str, Any]] = []
        for i in top_idx:
            s = float(scores[int(i)])
            if s < min_s:
                continue
            out.append({
                "name": _TOOL_NAMES[int(i)],
                "description": _TOOL_DESCRIPTIONS[int(i)],
                "score": round(s, 3),
            })
        return out
    except Exception as exc:
        logger.debug("tool_router.rank: %s", exc)
        return []


def build_tool_hint_block(query: str, *, top_k: Optional[int] = None) -> Optional[str]:
    """Construct the SYSTEM message text suggesting relevant tools.

    Returns None when there is nothing useful to surface (router
    disabled, no tools above threshold, etc.) so callers can skip
    injecting an empty block.
    """
    ranked = rank_tools_for_query(query, top_k=top_k)
    if not ranked:
        return None
    lines = [
        "TOOL ROUTER (semantic suggestion based on the user's query):",
        "For this turn, the most relevant tools from your registry are:",
    ]
    for r in ranked:
        # Trim description to keep the system message compact.
        desc = (r["description"] or "").strip()
        # The description already starts with "{name}." — strip duplicate.
        if desc.lower().startswith((r["name"] + ".").lower()):
            desc = desc[len(r["name"]) + 1:].strip()
        if len(desc) > 180:
            desc = desc[:177] + "..."
        lines.append(f"  - {r['name']} (score={r['score']:.2f}): {desc}")
    lines.append(
        "If your task matches any of these, PREFER calling the tool over "
        "narrating or disavowing. If none look right, you may call "
        "`introspect_tools` to query the full registry directly."
    )
    return "\n".join(lines)


def snapshot() -> Dict[str, Any]:
    """Diagnostic snapshot for /v1/_debug/agentic."""
    out: Dict[str, Any] = {
        "enabled": _enabled(),
        "top_k_default": _top_k_default(),
        "min_score": _min_score(),
        "embedder_ready": _get_embedder() is not None,
        "embeddings_cached_count": len(_TOOL_NAMES),
        "registry_hash": _REGISTRY_HASH,
        "cache_path": str(_cache_path()),
        "cache_exists": _cache_path().exists(),
    }
    return out
