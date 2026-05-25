"""Complexity × vault-confidence hybrid router (Round 5.7).

Picks a model tier per turn using TWO signals OpenJarvis already produces:
  1. complexity score (0.0-1.0) — from `learning.routing.complexity`
  2. vault confidence (0.0-1.0) — Jaccard overlap of query tokens vs
     MEMORY.md tokens (since memory_manage is CRUD-only, no recall scoring)

Tiers (per original Round 3.4 plan):
    free-llama          — high vault + low complexity (cheapest, fastest)
    gemini-flash        — low vault + low complexity (cheap fallback)
    orchestrator-fastest — medium complexity (parallel ensemble, longest wins)
    orchestrator-consensus — high complexity ≥ 0.7 (Jaccard-agreement pick)

Hermes routes by a single ladder (1 → 2 → 3); this picker uses 2D state
and only ever calls consensus on the ~5% of queries that need it, so the
latency cost is bounded.

Public API:
    pick_tier(query, complexity, vault_signal) -> Tier
    vault_confidence(query) -> float  (cached 60s)
    decide(query, complexity_info) -> dict (full debug record)

Env gate: OPENJARVIS_HYBRID_ROUTING_ENABLED (default false until verified).
Env tuning: OPENJARVIS_HYBRID_COMPLEXITY_CONSENSUS (default 0.7)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


# Tier labels — also the value of an outgoing log marker so we can grep
# production traffic for tier mix.
TIER_FREE_LLAMA = "free-llama"
TIER_GEMINI_FLASH = "gemini-flash"
TIER_ORCH_FASTEST = "orchestrator-fastest"
TIER_ORCH_CONSENSUS = "orchestrator-consensus"

# Model identifiers to forward to the engine when a tier is selected.
TIER_MODELS = {
    TIER_FREE_LLAMA: "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    TIER_GEMINI_FLASH: "openrouter/google/gemini-2.5-flash",
    TIER_ORCH_FASTEST: None,    # signals "use orchestrator"
    TIER_ORCH_CONSENSUS: None,  # signals "use orchestrator + consensus mode"
}


# ---------------------------------------------------------------------------
# Vault confidence — Jaccard against MEMORY.md
# ---------------------------------------------------------------------------

_TOK_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "with", "for", "to",
    "of", "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "be", "been", "have", "has", "had", "do", "does", "did", "i", "me", "my",
    "you", "your", "he", "she", "it", "we", "us", "they", "them", "this",
    "that", "these", "those",
})

_MEMORY_LOCK = threading.Lock()
_MEMORY_TOKENS: Optional[Set[str]] = None
_MEMORY_TS: float = 0.0
_MEMORY_TTL_SEC = 60.0


def _memory_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    return d / "MEMORY.md"


def _tokens(text: str) -> Set[str]:
    return {t for t in _TOK_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 2}


def _load_memory_tokens() -> Set[str]:
    """Return cached set of tokens in MEMORY.md. Refreshes every 60s."""
    global _MEMORY_TOKENS, _MEMORY_TS
    now = time.time()
    with _MEMORY_LOCK:
        if _MEMORY_TOKENS is not None and (now - _MEMORY_TS) < _MEMORY_TTL_SEC:
            return _MEMORY_TOKENS
        try:
            p = _memory_path()
            if p.exists():
                _MEMORY_TOKENS = _tokens(p.read_text(encoding="utf-8", errors="ignore"))
            else:
                _MEMORY_TOKENS = set()
        except Exception:
            _MEMORY_TOKENS = set()
        _MEMORY_TS = now
        return _MEMORY_TOKENS


def vault_confidence(query: str) -> float:
    """Jaccard overlap between query tokens and MEMORY.md tokens. 0..1."""
    if not query:
        return 0.0
    q_tokens = _tokens(query)
    if not q_tokens:
        return 0.0
    m_tokens = _load_memory_tokens()
    if not m_tokens:
        return 0.0
    return round(len(q_tokens & m_tokens) / max(len(q_tokens), 1), 3)


# ---------------------------------------------------------------------------
# Tier picker
# ---------------------------------------------------------------------------

@dataclass
class TierDecision:
    tier: str
    model: Optional[str]
    complexity: float
    vault: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_HYBRID_ROUTING_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _consensus_threshold() -> float:
    try:
        return float(os.environ.get("OPENJARVIS_HYBRID_COMPLEXITY_CONSENSUS", "0.7"))
    except Exception:
        return 0.7


def pick_tier(query: str, complexity: float, vault: float) -> TierDecision:
    """Pure function: given the two signals, return a tier.

    Decision matrix (intentional overlap zones — see `reason` for which
    branch fired):
                       complexity
                low    │ med    │ high (≥0.7)
            ┌─────────┼────────┼─────────────┐
       vault│ high    │ free-llama  │ orchestrator-fastest │ orchestrator-consensus
            ├────────┼────────────┼─────────────────────┼────────────────────────┤
            │ low    │ gemini-flash │ orchestrator-fastest │ orchestrator-consensus
            └────────┴────────────┴─────────────────────┴────────────────────────┘
    """
    c = max(0.0, min(1.0, float(complexity or 0.0)))
    v = max(0.0, min(1.0, float(vault or 0.0)))
    thresh = _consensus_threshold()

    # Tier 3 — consensus always wins for high complexity, regardless of vault.
    if c >= thresh:
        return TierDecision(
            tier=TIER_ORCH_CONSENSUS,
            model=TIER_MODELS[TIER_ORCH_CONSENSUS],
            complexity=c, vault=v,
            reason=f"complexity {c:.2f} >= consensus threshold {thresh:.2f}",
        )
    # Tier 2 — medium complexity uses orchestrator fastest (regardless of vault)
    if c >= 0.4:
        return TierDecision(
            tier=TIER_ORCH_FASTEST,
            model=TIER_MODELS[TIER_ORCH_FASTEST],
            complexity=c, vault=v,
            reason=f"medium complexity {c:.2f} → ensemble fastest",
        )
    # Low complexity branch: split on vault confidence
    if v >= 0.20:
        return TierDecision(
            tier=TIER_FREE_LLAMA,
            model=TIER_MODELS[TIER_FREE_LLAMA],
            complexity=c, vault=v,
            reason=f"low complexity {c:.2f} + high vault {v:.2f} → free llama",
        )
    return TierDecision(
        tier=TIER_GEMINI_FLASH,
        model=TIER_MODELS[TIER_GEMINI_FLASH],
        complexity=c, vault=v,
        reason=f"low complexity {c:.2f} + low vault {v:.2f} → gemini flash",
    )


def decide(query: str, complexity_score: float = 0.0) -> Optional[TierDecision]:
    """Convenience wrapper: returns None if disabled so callers can no-op."""
    if not _enabled():
        return None
    v = vault_confidence(query)
    decision = pick_tier(query, complexity_score, v)
    logger.info(
        "openjarvis.hybrid.pick tier=%s c=%.2f v=%.2f reason=%r",
        decision.tier, decision.complexity, decision.vault, decision.reason,
    )
    return decision


def snapshot() -> Dict[str, Any]:
    """For /v1/_debug/agentic — shows config + sample decisions."""
    out: Dict[str, Any] = {"enabled": _enabled()}
    out["consensus_threshold"] = _consensus_threshold()
    out["memory_tokens_cached"] = len(_load_memory_tokens())
    # Sample decisions across the spectrum
    samples = []
    for c, label in ((0.1, "trivial"), (0.5, "medium"), (0.85, "complex")):
        d = pick_tier("sample query", c, 0.0)
        samples.append({"complexity": c, "label": label, "tier": d.tier})
    out["sample_decisions"] = samples
    return out
