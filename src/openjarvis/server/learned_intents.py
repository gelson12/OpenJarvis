"""Learned intents — the SECOND half of the self-improvement loop.

THE WHOLE POINT
---------------
The disavowal_detector logs every "I don't have that tool" event with
the category (email/calendar/watch/unknown) and the user's exact
phrasing. This module READS that log periodically, clusters similar
failing phrasings, and AUTO-PROMOTES a new regex pattern into
``~/.openjarvis/learned_intents.json``.

intent_preexec.py checks the learned-intents store BEFORE its hardcoded
regex on every chat turn. So:

  Turn 1: user says "verify the last email today"
          → no match in hardcoded regex
          → LLM disavows
          → disavowal_detector logs it (category=email)

  (auto-promoter wakes up, sees 3+ similar disavowals tagged "email"
   with the verb "verify", writes a new pattern into learned_intents.json)

  Turn 2 (or 3rd, or 5th): user says same/similar thing
          → intent_preexec checks learned_intents FIRST
          → matches the auto-learned pattern
          → pre-executes outlook_list_messages directly
          → REAL DATA returned
          → no disavowal possible

Cluster threshold: 3 disavowals with overlapping vocabulary (>=50%
shared significant tokens) → promote.

Public API:
    load_patterns() → dict[category, list[regex_pattern_str]]
    match_category(text) → "email" | "calendar" | "watch" | None
    promote_from_disavowals() → list of newly-promoted patterns
    start_promoter_daemon() → starts the background loop

Env:
    OPENJARVIS_LEARNED_INTENTS_ENABLED       default true
    OPENJARVIS_LEARNED_PROMOTE_THRESHOLD     default 3 (clusters of this size promote)
    OPENJARVIS_LEARNED_PROMOTER_INTERVAL_SEC default 300 (5 min cadence)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_PROMOTER_THREAD: Optional[threading.Thread] = None


# Round 8: domains that have a server-side tool pre-executor wired in
# ``intent_preexec``. For these, learned patterns dispatch via
# ``action="preexec"`` (fire the tool directly). For every other domain
# (history, science, code, math, language, logic, multistep, automation,
# geography, general, …) the action is ``"prompt_hint"`` — we inject a
# learned hint as a system message instead.
_PREEXEC_DOMAINS = frozenset({"email", "calendar", "watch"})


_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "with", "for", "to",
    "of", "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "be", "been", "have", "has", "had", "do", "does", "did", "i", "me", "my",
    "you", "your", "we", "us", "they", "them", "this", "that", "these",
    "those", "can", "could", "would", "should", "will", "shall", "may",
    "might", "must", "please", "today", "tomorrow",
})

_TOK_RE = re.compile(r"[a-z][a-z0-9'-]{1,}")


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_LEARNED_INTENTS_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _threshold() -> int:
    try:
        return max(2, int(os.environ.get("OPENJARVIS_LEARNED_PROMOTE_THRESHOLD", "3")))
    except Exception:
        return 3


def _interval() -> int:
    try:
        return max(60, int(os.environ.get("OPENJARVIS_LEARNED_PROMOTER_INTERVAL_SEC", "300")))
    except Exception:
        return 300


def _jaccard_min() -> float:
    """Minimum Jaccard token overlap to group two disavowals into one
    cluster. Round 9.4 lowered the default from 0.5 to 0.35 because real
    English variants ('roman empire fall' / 'british empire collapse')
    share only ~2/6 = 0.33 tokens — the old threshold silently swallowed
    them. Exposed as env so it can be raised on noisy production data."""
    try:
        v = float(os.environ.get("OPENJARVIS_LEARNED_CLUSTER_JACCARD_MIN", "0.35"))
        # Clamp to a sane range to avoid pathological values
        return max(0.10, min(0.95, v))
    except Exception:
        return 0.35


def _store_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "learned_intents.json"


def _heartbeat_path() -> Path:
    """File-based heartbeat so the daemon's liveness is observable from
    outside the uvicorn process (e.g. `railway ssh python -c snapshot`)."""
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    return d / "_promoter_heartbeat.json"


def _write_heartbeat() -> None:
    try:
        _heartbeat_path().write_text(
            json.dumps({"ts": time.time(),
                        "iso": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _read_heartbeat() -> Optional[Dict[str, Any]]:
    p = _heartbeat_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Read / write store
# ---------------------------------------------------------------------------

def _load_store() -> Dict[str, Any]:
    p = _store_path()
    if not p.exists():
        return {"patterns": {}, "history": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"patterns": {}, "history": []}


def _save_store(data: Dict[str, Any]) -> None:
    try:
        _store_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("learned_intents: save failed: %s", exc)


def load_patterns() -> Dict[str, List[str]]:
    """Return {category: [regex_str, ...]} for the intent_preexec to check."""
    if not _enabled():
        return {}
    data = _load_store()
    return data.get("patterns", {}) or {}


def match_category(text: str) -> Optional[str]:
    """Returns the first category whose learned pattern matches `text`, or
    None. Cheap hot-path call — used per chat turn by intent_preexec.

    Legacy string-returning API retained for backward compatibility:
    callers that need the action/hint metadata should call
    :func:`match_learned` instead.
    """
    m = match_learned(text)
    return m["domain"] if m else None


def match_learned(text: str) -> Optional[Dict[str, Any]]:
    """Round-8 universal matcher. Returns
    ``{"domain": str, "action": "preexec"|"prompt_hint", "hint_text": str|None,
       "regex": str}`` for the first hit, else None.

    Pattern entries in ``learned_intents.json`` may be either:
      * legacy string (regex only) — treated as ``action="preexec"`` for
        the three tool domains, ``action="prompt_hint"`` otherwise.
      * new dict ``{"regex","action","hint_text",...}`` — used as-is.
    """
    if not text or not _enabled():
        return None
    data = _load_store()
    patterns = data.get("patterns", {}) or {}
    if not patterns:
        return None
    for domain, entries in patterns.items():
        for entry in entries or []:
            if isinstance(entry, str):
                regex_str = entry
                action = "preexec" if domain in _PREEXEC_DOMAINS else "prompt_hint"
                hint_text: Optional[str] = None
            elif isinstance(entry, dict):
                regex_str = entry.get("regex") or ""
                action = entry.get("action") or (
                    "preexec" if domain in _PREEXEC_DOMAINS else "prompt_hint"
                )
                hint_text = entry.get("hint_text")
            else:
                continue
            if not regex_str:
                continue
            try:
                if re.search(regex_str, text, re.IGNORECASE):
                    return {
                        "domain": domain,
                        "action": action,
                        "hint_text": hint_text,
                        "regex": regex_str,
                    }
            except re.error:
                continue
    return None


# ---------------------------------------------------------------------------
# Clustering + promotion
# ---------------------------------------------------------------------------

def _significant_tokens(text: str) -> Set[str]:
    return {
        t for t in _TOK_RE.findall((text or "").lower())
        if t not in _STOP_WORDS and len(t) >= 3
    }


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def _domain_of(rec: Dict[str, Any]) -> str:
    """Backward-compat reader: pre-Round-8 records use 'inferred_category',
    new records use 'inferred_domain'. Treat 'unknown' as 'general' so
    legacy entries still cluster."""
    d = rec.get("inferred_domain") or rec.get("inferred_category") or "general"
    return "general" if d == "unknown" else d


def _cluster_disavowals(
    disavowals: List[Dict[str, Any]], domain: str,
) -> List[List[Dict[str, Any]]]:
    """Cluster disavowals within a domain into groups likely to be the
    same intent. Two-pass:

    1. Greedy Jaccard on significant tokens (threshold from env, default
       0.35). Cheap, offline, deterministic.
    2. If any disavowal didn't fit in a Jaccard cluster, ask
       obsidian-mind for semantically-similar past disavowals (handles
       cases where token overlap is low but meaning is the same, e.g.
       'last meeting I had' vs 'most recent calendar entry'). The
       semantic candidate set is treated as a cluster anchor and matched
       by user_text/ts against the local disavowal log.

    Falls back to pure Jaccard when Mind is unreachable."""
    by_dom = [d for d in disavowals if _domain_of(d) == domain]
    if not by_dom:
        return []
    tokens = [_significant_tokens(d.get("user_text", "")) for d in by_dom]
    threshold = _jaccard_min()
    clusters: List[List[int]] = []
    used: Set[int] = set()
    for i, ti in enumerate(tokens):
        if i in used or not ti:
            continue
        cluster_idx = [i]
        used.add(i)
        for j in range(i + 1, len(tokens)):
            if j in used:
                continue
            if _jaccard(ti, tokens[j]) >= threshold:
                cluster_idx.append(j)
                used.add(j)
        clusters.append(cluster_idx)

    # 9.3 — Semantic fallback for the orphans (disavowals that ended up
    # in singleton clusters). Ask obsidian-mind for similar past failures
    # by content embedding; if it returns ≥ threshold matches that are
    # ALSO in our local log, merge them in.
    try:
        from openjarvis.server import learning_mind as _mind
        if _mind._enabled():  # cheap env check; no network if disabled
            text_by_iso = {(d.get("iso") or ""): idx for idx, d in enumerate(by_dom)}
            text_by_user = {(d.get("user_text") or "").strip().lower(): idx
                            for idx, d in enumerate(by_dom)}
            for c_i, cluster in enumerate(clusters):
                if len(cluster) >= _threshold():
                    continue  # already big enough
                anchor_text = by_dom[cluster[0]].get("user_text", "")
                if not anchor_text:
                    continue
                candidates = _mind.semantic_cluster_candidates(
                    anchor_text, k=20, domain=domain,
                )
                merged_any = False
                for cand in candidates:
                    cand_text = (cand.get("user_text") or "").strip().lower()
                    if not cand_text:
                        continue
                    # Match by exact user_text OR by iso timestamp
                    j = text_by_user.get(cand_text)
                    if j is None and cand.get("iso"):
                        j = text_by_iso.get(cand["iso"])
                    if j is None or j in used or j in cluster:
                        continue
                    cluster.append(j)
                    used.add(j)
                    merged_any = True
                if merged_any:
                    logger.info(
                        "openjarvis.cluster.semantic_merged domain=%s new_size=%d",
                        domain, len(cluster),
                    )
    except Exception as exc:
        logger.debug("learned_intents: semantic fallback failed: %s", exc)

    return [[by_dom[i] for i in c] for c in clusters]


def _corpus_common_tokens(
    disavowals: List[Dict[str, Any]], domain: str,
) -> Set[str]:
    """Tokens that appear in >=80% of disavowals in this domain — they
    are too generic to be discriminative within the domain (e.g. "email"
    in every email disavowal). Derived dynamically rather than hardcoded
    so it scales to any new domain.

    Only kicks in once the domain has enough samples (>=5). For small
    corpora we keep every shared token — otherwise a 3-member cluster
    that shares its entire distinctive set would lose every candidate
    token and never promote."""
    members = [d for d in disavowals if _domain_of(d) == domain]
    if len(members) < 5:
        return set()
    freq: Dict[str, int] = defaultdict(int)
    for m in members:
        for tok in _significant_tokens(m.get("user_text", "")):
            freq[tok] += 1
    threshold = max(3, int(0.8 * len(members)))
    return {tok for tok, c in freq.items() if c >= threshold}


def _pattern_from_cluster(
    cluster: List[Dict[str, Any]],
    *,
    common_tokens_to_drop: Optional[Set[str]] = None,
) -> Optional[str]:
    """Extract a single discriminative regex from the cluster's user
    texts. Strategy: find the common significant tokens (intersection)
    and require them to appear in the matched text, separated by any
    other words. Falls back to None if no signal."""
    token_sets = [_significant_tokens(d.get("user_text", "")) for d in cluster]
    if not token_sets:
        return None
    common = set.intersection(*token_sets) if token_sets else set()
    # Drop tokens that are too generic within the surrounding domain
    # corpus (dynamically derived — no per-domain hardcoded list).
    if common_tokens_to_drop:
        common -= common_tokens_to_drop
    distinctive = sorted(common, key=lambda t: -len(t))[:3]
    if not distinctive:
        return None
    # Build: (?=.*\bword1\b)(?=.*\bword2\b).+ — order-independent.
    parts = [f"(?=.*\\b{re.escape(t)}\\b)" for t in distinctive]
    pattern = "^" + "".join(parts) + ".+"
    return pattern


def _entry_regex(entry: Any) -> str:
    """Read the regex out of a store entry (string or dict)."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("regex") or ""
    return ""


def _memory_writeback(promo: Dict[str, Any]) -> None:
    """Append a learning line to MEMORY.md so the user can see what was
    learned. Disabled by env: OPENJARVIS_MEMORY_WRITEBACK_ON_PROMOTION.
    Best-effort — failures must not block promotion."""
    if os.environ.get("OPENJARVIS_MEMORY_WRITEBACK_ON_PROMOTION", "true").lower() not in (
        "1", "true", "yes", "on",
    ):
        return
    try:
        from openjarvis.tools.memory_manage import MemoryManageTool
        sample = (promo.get("sample_phrases") or [""])[0][:100]
        ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = (
            f"[{ymd}] Learned: domain={promo['domain']} "
            f"action={promo['action']} matched {promo['member_count']} past "
            f"failures. Sample: {sample!r}."
        )
        MemoryManageTool()._add(entry)
    except Exception as exc:
        logger.debug("learned_intents: memory writeback failed: %s", exc)


def _vault_writeback(promo: Dict[str, Any]) -> None:
    """Mirror the promotion to the Obsidian vault for human review.
    Best-effort — vault outages must not block promotion."""
    if os.environ.get("OPENJARVIS_LEARNING_VAULT_MIRROR_ENABLED", "true").lower() not in (
        "1", "true", "yes", "on",
    ):
        return
    if not os.environ.get("OBSIDIAN_VAULT_URL", "").strip():
        return
    try:
        from openjarvis.integrations.obsidian_vault import (
            get_default_client,
            VaultUnavailableError,
        )
        client = get_default_client()
        ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        domain = promo["domain"]
        # Round 9.1: slug from the first sample user_text, NOT the regex.
        # The regex contains `\b` word-boundary anchors that leak as the
        # letter "b" through the slugifier (e.g. "bempire-b-collapse").
        sample = ""
        if promo.get("sample_phrases"):
            sample = promo["sample_phrases"][0] or ""
        if not sample:
            sample = promo.get("pattern") or "pattern"
        slug = re.sub(r"[^a-z0-9]+", "-", sample.lower()).strip("-")[:50] or "pattern"
        path = f"brain/jarvis-learnings/{domain}/{ymd}-{slug}.md"
        body_lines = [
            f"# Learned pattern — {domain}",
            "",
            f"- **Action:** {promo['action']}",
            f"- **Regex:** `{promo['pattern']}`",
            f"- **Member count:** {promo['member_count']}",
            f"- **Promoted at:** {promo['promoted_at']}",
            "",
            "## Sample failing phrases",
            "",
        ]
        for s in promo.get("sample_phrases", [])[:5]:
            body_lines.append(f"- {s}")
        if promo.get("hint_text"):
            body_lines += ["", "## Auto-drafted hint", "", promo["hint_text"]]
        try:
            client.write_file(path, "\n".join(body_lines) + "\n")
        except VaultUnavailableError as exc:
            logger.debug("learned_intents: vault write_file unavailable: %s", exc)
            return
        # Append to a rolling index for fast human review.
        try:
            line = f"- [{ymd}] **{domain}** ({promo['action']}, n={promo['member_count']}) — [[{path}]]\n"
            client.append_to_file("brain/jarvis-learnings/INDEX.md", line)
        except VaultUnavailableError:
            pass
    except Exception as exc:
        logger.debug("learned_intents: vault writeback failed: %s", exc)


def promote_from_disavowals() -> List[Dict[str, Any]]:
    """Read recent disavowals, cluster them across ALL domains (Round 8 —
    universal), and add promoted patterns to the store. Returns the list
    of newly-promoted patterns."""
    if not _enabled():
        return []
    try:
        from openjarvis.server import disavowal_detector as _dd
    except Exception:
        return []
    disavowals = _dd.read_recent(limit=500)
    if not disavowals:
        return []
    # Dynamic domain set — every domain that appears in the log gets a
    # promotion pass (history, science, code, math, etc.), not just the
    # legacy email/calendar/watch trio.
    domains_in_log = sorted({_domain_of(d) for d in disavowals if _domain_of(d)})
    threshold = _threshold()
    new_promotions: List[Dict[str, Any]] = []
    # Late imports so the module loads cleanly even when these aren't
    # installed (every helper below is best-effort by contract).
    try:
        from openjarvis.server import learned_prompt_hints as _hints
    except Exception:
        _hints = None  # type: ignore[assignment]
    try:
        from openjarvis.server import learning_pg as _pg
    except Exception:
        _pg = None  # type: ignore[assignment]

    with _LOCK:
        store = _load_store()
        patterns = store.setdefault("patterns", {})
        history = store.setdefault("history", [])
        for domain in domains_in_log:
            common_drop = _corpus_common_tokens(disavowals, domain)
            clusters = _cluster_disavowals(disavowals, domain)
            for cluster in clusters:
                if len(cluster) < threshold:
                    continue
                pat = _pattern_from_cluster(cluster, common_tokens_to_drop=common_drop)
                if not pat:
                    continue
                existing_entries = patterns.get(domain, []) or []
                if any(_entry_regex(e) == pat for e in existing_entries):
                    continue  # already promoted

                action = "preexec" if domain in _PREEXEC_DOMAINS else "prompt_hint"
                hint_text: Optional[str] = None
                if action == "prompt_hint" and _hints is not None:
                    try:
                        hint_text = _hints.draft_hint_for_cluster(cluster, domain)
                        _hints.add_hint(
                            domain=domain,
                            regex=pat,
                            hint_text=hint_text,
                            member_count=len(cluster),
                        )
                    except Exception as exc:
                        logger.debug("hint draft failed: %s", exc)

                # Persist into the unified store as a dict entry so the
                # runtime matcher can dispatch on action.
                entry = {
                    "regex": pat,
                    "action": action,
                    "hint_text": hint_text,
                    "member_count": len(cluster),
                    "promoted_at": datetime.now(timezone.utc).isoformat(),
                }
                existing_entries.append(entry)
                patterns[domain] = existing_entries

                promo = {
                    "promoted_at": entry["promoted_at"],
                    "domain": domain,
                    # Keep legacy 'category' alias for daily_self_report
                    "category": domain,
                    "action": action,
                    "hint_text": hint_text,
                    "pattern": pat,
                    "member_count": len(cluster),
                    "sample_phrases": [c.get("user_text", "")[:120] for c in cluster[:5]],
                }
                history.append(promo)
                new_promotions.append(promo)
                logger.warning(
                    "openjarvis.learned_intents.promoted domain=%s action=%s "
                    "pattern=%r members=%d",
                    domain, action, pat, len(cluster),
                )

                # Round 8.7 — durable mirrors. Each is best-effort.
                if _pg is not None:
                    try:
                        _pg.mirror_pattern(
                            domain=domain,
                            regex=pat,
                            action=action,
                            hint_text=hint_text,
                            member_count=len(cluster),
                            promoted_at=entry["promoted_at"],
                        )
                    except Exception as exc:
                        logger.debug("pg mirror_pattern failed: %s", exc)
                _vault_writeback(promo)
                _memory_writeback(promo)

        if new_promotions:
            _save_store(store)
    return new_promotions


# ---------------------------------------------------------------------------
# Background daemon
# ---------------------------------------------------------------------------

def _promoter_loop() -> None:
    interval = _interval()
    logger.info("openjarvis.learned_intents.promoter started interval=%ds", interval)
    # Write initial heartbeat so snapshot reports the daemon is alive
    # immediately after boot, not after the first full interval.
    _write_heartbeat()
    while True:
        try:
            time.sleep(interval)
        except Exception:
            time.sleep(300)
        if not _enabled():
            _write_heartbeat()
            continue
        try:
            promotions = promote_from_disavowals()
            if promotions:
                logger.warning(
                    "openjarvis.learned_intents.promoter_run promoted=%d",
                    len(promotions),
                )
        except Exception as exc:
            logger.warning("learned_intents.promoter run failed: %s", exc)
        # Every successful iteration (including no-op runs) updates the
        # heartbeat. Out-of-process snapshot callers read this file.
        _write_heartbeat()


def start_promoter_daemon() -> None:
    """Idempotent — starts the daemon thread on first call."""
    global _PROMOTER_THREAD
    if _PROMOTER_THREAD is not None and _PROMOTER_THREAD.is_alive():
        return
    if not _enabled():
        logger.info("openjarvis.learned_intents.promoter disabled by env")
        return
    t = threading.Thread(
        target=_promoter_loop,
        name="openjarvis-learned-intents-promoter",
        daemon=True,
    )
    t.start()
    _PROMOTER_THREAD = t


def snapshot() -> Dict[str, Any]:
    store = _load_store()
    patterns = store.get("patterns", {})
    history = store.get("history", [])
    per_domain_count = {dom: len(pats or []) for dom, pats in patterns.items()}
    # Round 9.2 — heartbeat-based liveness check, observable from any process.
    hb = _read_heartbeat() or {}
    hb_ts = hb.get("ts")
    secs_since = (time.time() - hb_ts) if isinstance(hb_ts, (int, float)) else None
    interval_sec = _interval()
    # daemon_alive is True if a heartbeat landed within 2x the configured
    # interval (with a 30s grace) — covers normal pauses without flagging
    # a healthy daemon as dead.
    daemon_alive = (
        secs_since is not None and secs_since <= (2 * interval_sec + 30)
    )
    return {
        "enabled": _enabled(),
        "threshold": _threshold(),
        "interval_sec": interval_sec,
        # New canonical key. Keep 'categories' alias for backward compat
        # with /v1/_debug/agentic consumers.
        "domains": per_domain_count,
        "categories": per_domain_count,
        "preexec_domains": sorted(_PREEXEC_DOMAINS),
        "total_patterns": sum(len(p or []) for p in patterns.values()),
        "total_promotions_ever": len(history),
        "recent_promotions": history[-5:],
        # In-process flag (legacy) — only truthful in the uvicorn worker
        "daemon_running": _PROMOTER_THREAD is not None and _PROMOTER_THREAD.is_alive(),
        # Out-of-process heartbeat — truthful from any caller
        "daemon_alive_heartbeat": daemon_alive,
        "seconds_since_last_heartbeat": secs_since,
        "last_heartbeat_iso": hb.get("iso"),
    }
