"""Lightweight query → domain classifier (Round 5.6).

Cheap deterministic classifier used by the reflector (so model_preference
learns per-domain), the hybrid router (tier selection), the prompt
evolver (which variant to load), and the answer cache (cache namespacing).

No LLM call — pure regex + keyword scoring. The point is to be fast and
predictable, not perfect. If the classifier is wrong the system falls
back to "general" gracefully.

Public API:
    infer_domain(text, complexity_signals=None, enabled_groups=None) -> str

Domains emitted (matches the mini-eval suite labels for easy correlation):
    math | code | geography | history | science | language | logic |
    multistep | automation | general
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


_KEYWORDS: List = [
    # (domain, compiled regex)
    ("code",       re.compile(r"\b(python|javascript|typescript|java|rust|sql|regex|function|class|api|repo|github|docker|kubernetes|npm|pip|stack ?trace|exception|debug|refactor|merge|commit|deploy|compile)\b", re.I)),
    ("math",       re.compile(r"\b(compute|calculate|equation|integral|derivative|matrix|probability|percent|square root|sum of|product of|divide|multiply)\b|\d+\s*[\+\-\*\/\^]\s*\d+|[÷×]|^\s*[\d\.\(\)\+\-\*\/\^=\s]+\s*\?*\s*$", re.I)),
    ("geography",  re.compile(r"\b(capital|continent|country|ocean|river|mountain|city|nation|hemisphere|equator|latitude|longitude|map of)\b", re.I)),
    ("history",    re.compile(r"\b(history|historical|century|empire|dynasty|revolution|world ?war|invented|discovered|founded|first to|ancient|medieval|renaissance|painted)\b|\b1[0-9]{3}\b|\b20[0-2][0-9]\b", re.I)),
    ("science",    re.compile(r"\b(atom|molecule|chemical|physics|biology|gravity|cell|species|photosynthesis|element|symbol for|planet|solar|orbit|electron|proton|dna|protein)\b", re.I)),
    ("language",   re.compile(r"\b(translate|past tense|plural|grammar|spell|spelled|spelling|pronoun|verb|noun|adjective|in spanish|in french|in german|in chinese|in japanese)\b", re.I)),
    ("logic",      re.compile(r"\b(if|then|therefore|all .* are|some .* are|none .* are|true or false|syllogism|implies|contradiction|paradox|train .* leaves|how (?:far|long|fast))\b", re.I)),
    ("automation", re.compile(r"\b(automate|workflow|n8n|webhook|cron|schedule|trigger|zapier|integration|connector|pipeline|sync)\b", re.I)),
    ("multistep",  re.compile(r"\b(list|name|enumerate|step ?by ?step|first .* then|order|sequence)\b|\b(\d+\s+(steps|items|things|reasons|examples))\b", re.I)),
]


def _from_signals(signals: Optional[Dict[str, Any]]) -> Optional[str]:
    if not signals:
        return None
    if signals.get("has_code"):
        return "code"
    if signals.get("has_math"):
        return "math"
    return None


def _from_enabled_groups(groups: Optional[Iterable[str]]) -> Optional[str]:
    if not groups:
        return None
    g = set(s.lower() for s in groups if isinstance(s, str))
    if "n8n" in g or "automation" in g:
        return "automation"
    if "github" in g or "code" in g:
        return "code"
    return None


def infer_domain(text: str,
                 complexity_signals: Optional[Dict[str, Any]] = None,
                 enabled_groups: Optional[Iterable[str]] = None) -> str:
    """Return a single domain string. Always succeeds; defaults to 'general'."""
    if not text or not text.strip():
        return "general"

    # Priority 1: explicit complexity signals (most reliable when present)
    d = _from_signals(complexity_signals)
    if d:
        return d

    # Priority 2: enabled tool groups for this request
    d = _from_enabled_groups(enabled_groups)
    if d:
        return d

    # Priority 3: keyword regex match. First match wins; the keyword list is
    # ordered by specificity (most-specific first).
    text_short = text[:600]
    for domain, pat in _KEYWORDS:
        if pat.search(text_short):
            return domain

    return "general"


def all_known_domains() -> List[str]:
    """Return the ordered list of recognised domains (plus 'general')."""
    return [d for d, _ in _KEYWORDS] + ["general"]
