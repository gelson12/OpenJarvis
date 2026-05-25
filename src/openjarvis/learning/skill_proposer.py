"""Auto-skill-proposer (Round 5 BONUS-C — outclass Hermes #3).

Reads the corpus-shim distiller's failure records, clusters them by
(domain, top-keywords), and when a cluster has >=3 similar failures
drafts a candidate TOML skill into ~/.openjarvis/skills/proposed/.

These are PROPOSED skills only — not auto-merged into the bundled
SkillManager corpus. A human reviews them and (if good) moves them to
the main skills/data/ directory.

Why this beats Hermes: Hermes has no skill library at all; OpenJarvis
has 20 bundled skills + can grow them automatically from production
failure patterns.

Public API:
    propose_for_all_domains() -> List[Path]  # called by scheduler
    snapshot() -> dict                       # for /v1/_debug/agentic

Env gate: OPENJARVIS_SKILL_PROPOSER_ENABLED (default false).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_MIN_CLUSTER_SIZE = 3
_MAX_PROPOSED_PER_DAY = 5

_TOK_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "with", "for", "to",
    "of", "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "this", "that", "these", "those", "what", "when", "where", "why", "how",
    "can", "do", "does", "did", "will", "should", "would", "could",
})


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_SKILL_PROPOSER_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _proposed_dir() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    d = d / "skills" / "proposed"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _tokens(text: str) -> List[str]:
    return [t for t in _TOK_RE.findall((text or "").lower()) if t not in _STOP]


def _cluster_by_keywords(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group records by their top-3 shared keywords (joined)."""
    keyed: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        toks = _tokens(rec.get("user", ""))
        if len(toks) < 2:
            continue
        # Top 3 by frequency in this single query, lex order
        counter = Counter(toks)
        top3 = sorted([t for t, _ in counter.most_common(6)][:3])
        key = "-".join(top3)
        if not key:
            continue
        keyed.setdefault(key, []).append(rec)
    return keyed


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:48] or "skill"


def _draft_toml(cluster_key: str, records: List[Dict[str, Any]], domain: str) -> str:
    """Produce a TOML skill draft from a cluster of similar failed queries."""
    name = _slugify(cluster_key)
    iso = datetime.now(timezone.utc).isoformat()
    example_queries = [r.get("user", "")[:200] for r in records[:5]]
    summary = (
        f"Auto-proposed skill from {len(records)} similar failed turns "
        f"in domain={domain!r}. Keywords: {cluster_key.replace('-', ', ')}."
    )
    lines = [
        f'# Auto-proposed by skill_proposer @ {iso}',
        f'# Domain: {domain}',
        f'# Failures observed: {len(records)}',
        f'# Common keywords: {cluster_key}',
        '',
        '[skill]',
        f'name = "{name}"',
        f'description = "{summary}"',
        f'domain = "{domain}"',
        'status = "proposed"',
        '',
        '[examples]',
        'queries = [',
    ]
    for q in example_queries:
        safe = q.replace('"', '\\"').replace('\n', ' ')[:180]
        lines.append(f'    "{safe}",')
    lines.append(']')
    lines.append('')
    lines.append('# TODO(human-review): fill in steps, tool_calls, expected_output.')
    lines.append('# When ready, move this file to src/openjarvis/skills/data/ to enable.')
    return "\n".join(lines)


def propose_for_all_domains() -> List[Path]:
    """Scan the recent corpus, cluster failures, write proposed skills."""
    if not _enabled():
        return []
    try:
        from openjarvis.learning import online_distiller as _dist
    except Exception as exc:
        logger.debug("skill_proposer: corpus import failed: %s", exc)
        return []

    out: List[Path] = []
    dir_proposed = _proposed_dir()
    # Budget per day to avoid proposal storm
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing_today = len(list(dir_proposed.glob(f"{today}_*.toml")))
    budget = max(0, _MAX_PROPOSED_PER_DAY - existing_today)
    if budget == 0:
        return []

    # Pull all recent failures, group by domain
    records = _dist.recent_failures(domain=None, limit=200)
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        by_domain.setdefault(rec.get("domain", "general"), []).append(rec)

    for domain, recs in by_domain.items():
        if budget == 0:
            break
        clusters = _cluster_by_keywords(recs)
        for key, cluster in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            if len(cluster) < _MIN_CLUSTER_SIZE:
                continue
            if budget == 0:
                break
            slug = _slugify(key)
            target = dir_proposed / f"{today}_{domain}_{slug}.toml"
            if target.exists():
                continue
            try:
                toml = _draft_toml(key, cluster, domain)
                target.write_text(toml, encoding="utf-8")
                out.append(target)
                budget -= 1
                logger.info(
                    "openjarvis.skill_proposer.draft path=%s domain=%s cluster=%d",
                    target.name, domain, len(cluster),
                )
            except Exception as exc:
                logger.debug("skill_proposer: write failed: %s", exc)
    return out


def snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": _enabled()}
    try:
        d = _proposed_dir()
        out["dir"] = str(d)
        files = sorted(d.glob("*.toml"))
        out["total_proposed"] = len(files)
        if files:
            out["latest"] = [f.name for f in files[-3:]]
    except Exception as exc:
        out["error"] = str(exc)
    return out
