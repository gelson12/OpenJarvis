"""Quality-aware response picker for the parallel-LLM race.

Default `pick_best` picks by raw length (longer ≈ more complete — weak
proxy). This module implements a smarter picker:

  1. EMBEDDING PRE-FILTER — compute embeddings for all clean responses,
     find the centroid, pick the top-N closest. This drops semantic
     outliers (one provider wildly diverging from the consensus).
     Embeddings via Gemini text-embedding-004 (free, 1500 RPM).

  2. LLM JUDGE — give a small fast judge model the user's question + the
     top-N pre-filtered responses, ask it to pick the best one with a
     one-sentence rationale. Judge model preference order:
        Groq (free, 30 RPM, fast)
        → Cerebras (free, fastest open-weight inference)
        → Gemini Flash (paid, ~$0.0001/turn)

  3. FALLBACK — if both pre-filter and judge fail (network, no keys),
     return the original `pick_best` longest-wins result.

Public API:
    pick_quality(question, responses, *, top_n=3) -> dict | None

Env gate: OPENJARVIS_QUALITY_PICK_ENABLED (default false; opt-in until
verified on real traffic).
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_QUALITY_PICK_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


# ----------------------------------------------------------------------
# Step 1 — Embedding pre-filter (centroid-based outlier drop)
# ----------------------------------------------------------------------

async def _embed_gemini(session: aiohttp.ClientSession, texts: list[str]) -> Optional[list[list[float]]]:
    """Batch-embed texts via Gemini text-embedding-004. Free tier.
    Returns list of float vectors aligned with `texts`, or None on failure."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not texts:
        return None
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "text-embedding-004:batchEmbedContents?key=" + api_key
    )
    payload = {
        "requests": [
            {
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": t[:2000]}]},
            }
            for t in texts
        ]
    }
    try:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=4.0)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        embs = data.get("embeddings") or []
        if len(embs) != len(texts):
            return None
        return [e.get("values") or [] for e in embs]
    except Exception as exc:  # noqa: BLE001
        logger.debug("quality_pick: gemini embed failed: %s", exc)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _centroid(vecs: list[list[float]]) -> list[float]:
    if not vecs:
        return []
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(dim)]


async def _filter_by_centroid(
    session: aiohttp.ClientSession,
    responses: list[dict],
    top_n: int,
) -> list[dict]:
    """Embed all responses, drop the semantic outliers, return top-N by
    cosine similarity to the centroid. Falls back to length-sort on
    embedding failure."""
    texts = [(r.get("text") or "") for r in responses]
    vecs = await _embed_gemini(session, texts)
    if not vecs:
        # No embeddings — fall back to length-sorted top-N
        return sorted(responses, key=lambda r: -len(r.get("text") or ""))[:top_n]
    centroid = _centroid(vecs)
    scored = [
        (i, _cosine(vecs[i], centroid))
        for i in range(len(responses))
    ]
    scored.sort(key=lambda kv: -kv[1])
    return [responses[i] for i, _ in scored[:top_n]]


# ----------------------------------------------------------------------
# Step 2 — LLM judge
# ----------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are an answer-quality judge. Given a user question and N candidate "
    "answers, pick the BEST one based on: factual accuracy, completeness, "
    "directness, and absence of hedging or hallucination. Respond with "
    "ONLY a single digit (1, 2, or 3) indicating your pick — nothing else, "
    "no explanation, no JSON, no surrounding text."
)


def _build_judge_prompt(question: str, candidates: list[dict]) -> str:
    lines = [f"USER QUESTION:\n{(question or '')[:600]}", "", "CANDIDATES:"]
    for i, c in enumerate(candidates, 1):
        text = (c.get("text") or "")[:800]
        lines.append(f"\n--- Answer {i} (from {c.get('model', '?')}) ---")
        lines.append(text)
    lines.append("\nWhich answer is best? Reply with ONLY the number 1, 2, or 3.")
    return "\n".join(lines)


async def _judge_via_groq(session: aiohttp.ClientSession, prompt: str) -> Optional[int]:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 8,
                "temperature": 0,
            },
            timeout=aiohttp.ClientTimeout(total=4.0),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return _parse_pick(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quality_pick: groq judge failed: %s", exc)
        return None


async def _judge_via_cerebras(session: aiohttp.ClientSession, prompt: str) -> Optional[int]:
    api_key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        async with session.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama3.1-8b",
                "messages": [
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 8,
                "temperature": 0,
            },
            timeout=aiohttp.ClientTimeout(total=4.0),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return _parse_pick(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quality_pick: cerebras judge failed: %s", exc)
        return None


async def _judge_via_gemini(session: aiohttp.ClientSession, prompt: str) -> Optional[int]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + api_key
    )
    try:
        async with session.post(
            url,
            json={
                "system_instruction": {"parts": [{"text": _JUDGE_SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
            },
            timeout=aiohttp.ClientTimeout(total=4.0),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
        text = (
            ((data.get("candidates") or [{}])[0].get("content") or {})
            .get("parts", [{}])[0].get("text") or ""
        )
        return _parse_pick(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quality_pick: gemini judge failed: %s", exc)
        return None


def _parse_pick(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"[123]", text)
    if not m:
        return None
    return int(m.group(0))


async def _judge_pick(
    session: aiohttp.ClientSession,
    question: str,
    candidates: list[dict],
) -> Optional[int]:
    """Try free judges first (Groq → Cerebras), fall back to Gemini Flash.
    Returns 1-based index of winner, or None on total failure."""
    prompt = _build_judge_prompt(question, candidates)
    for label, fn in (("groq", _judge_via_groq),
                      ("cerebras", _judge_via_cerebras),
                      ("gemini", _judge_via_gemini)):
        pick = await fn(session, prompt)
        if pick is not None and 1 <= pick <= len(candidates):
            logger.info(
                "openjarvis.quality_pick.judge_won via=%s pick=%d of %d",
                label, pick, len(candidates),
            )
            return pick
    return None


# ----------------------------------------------------------------------
# Public entry
# ----------------------------------------------------------------------

async def pick_quality(
    question: str,
    responses: list[dict],
    *,
    top_n: int = 3,
) -> Optional[dict]:
    """Run the embedding-centroid pre-filter + LLM judge.

    Returns the chosen response dict, or None if quality picking failed
    (caller should fall back to legacy pick_best).
    """
    if not _enabled() or not responses:
        return None
    # Filter errors
    clean = [r for r in responses if r and (r.get("text") or "")
             and "Error" not in (r.get("text") or "")]
    if len(clean) < 2:
        # 0 or 1 clean response — quality picking adds no value
        return clean[0] if clean else None
    try:
        async with aiohttp.ClientSession() as session:
            # Step 1: embedding pre-filter
            candidates = await _filter_by_centroid(session, clean, top_n=top_n)
            if len(candidates) == 1:
                return candidates[0]
            # Step 2: LLM judge picks among the top-N
            pick = await _judge_pick(session, question, candidates)
            if pick is None:
                # Judge failed — return the centroid-closest as next-best
                return candidates[0]
            return candidates[pick - 1]
    except Exception as exc:  # noqa: BLE001
        logger.warning("openjarvis.quality_pick.failed: %s", exc)
        return None


def snapshot() -> dict:
    return {
        "enabled": _enabled(),
        "judge_chain": ["groq", "cerebras", "gemini"],
        "embedding_provider": "gemini-text-embedding-004",
        "estimated_cost_usd_per_turn": 0.0,
        "fallback_cost_usd_per_turn": 0.0001,
    }
