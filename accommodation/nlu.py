"""
Natural-language parsers for accommodation voice intents.

Pulled out of the worker handlers so OpenJarvis and Friday_Jarvis2 share the
same parsing rules. Pure functions, no I/O, fully testable.

Coverage as of Phase 2:
- Dates: "tonight", "tomorrow", "this/next weekend", "next Monday",
  "for 3 nights", "for a week", "from June 5 to June 8",
  "June 5th to 8th", "between June 5 and June 8".
- Guests: "for 3 adults", "for 4 people", "family of 4", "two of us",
  "me and my partner", "for a couple", "for one".
- Provider preference: "airbnb" → apify_airbnb, "hotel" → liteapi,
  otherwise empty (fan out to all configured providers).
- Location: noun phrase after "in/at/near/around" until next prep or punct.

When in doubt, return sensible defaults (next-Friday weekend, 2 guests,
no provider preference) rather than refusing — voice UX is forgiving.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

# ── Compile-once regex pool ──────────────────────────────────────────

_WEEKEND_RE = re.compile(r"\b(this|next)?\s*weekend\b", re.I)
_TONIGHT_RE = re.compile(r"\btonight\b", re.I)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)
_NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b", re.I)

# "for 3 nights" / "for a week" / "for the week" / "for the night"
_NIGHTS_RE = re.compile(
    r"\bfor\s+(?:the\s+)?(\d+|a|one|two|three|four|five|six|seven|eight|nine|ten|night|week|weekend)\s*"
    r"(nights?|days?|weeks?|weekends?)?\b",
    re.I,
)
_WORD_NUMS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Weekdays as anchors: "next Monday", "this Friday", "on Sunday"
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b(?:on\s+|this\s+|next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)

# Month names. Both full and abbreviated. Captures
# "June 5", "5th of June", "June 5 to 8", "from June 5 to June 8".
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTH_PATTERN = "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))

# "from June 5 to June 8" / "June 5 to 8" / "June 5 - June 8" / "5 June to 8 June"
_DATE_RANGE_RE = re.compile(
    rf"\b(?:from\s+|between\s+)?"
    rf"(?:({_MONTH_PATTERN})\s+)?"      # group 1: optional leading month
    rf"(\d{{1,2}})(?:st|nd|rd|th)?"     # group 2: first day
    rf"(?:\s+(?:of\s+)?({_MONTH_PATTERN}))?"  # group 3: trailing month for "5th of June"
    rf"\s*(?:to|–|-|until|through|and)\s*"
    rf"(?:({_MONTH_PATTERN})\s+)?"      # group 4: optional second month
    rf"(\d{{1,2}})(?:st|nd|rd|th)?"     # group 5: second day
    rf"(?:\s+(?:of\s+)?({_MONTH_PATTERN}))?",  # group 6: trailing month
    re.I,
)

# Multi-guest
_GUEST_NUM_RE = re.compile(
    r"\b(?:for\s+|with\s+)?(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:adults?|guests?|people|persons|of\s+us|grown[- ]?ups?)\b",
    re.I,
)
_FAMILY_RE = re.compile(
    r"\bfamily\s+of\s+(\d+|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.I,
)
_COUPLE_RE = re.compile(
    r"\b(?:for\s+)?(?:a\s+couple|two\s+of\s+us|me\s+and\s+my\s+(?:partner|wife|husband|boyfriend|girlfriend))\b",
    re.I,
)
# Bare "me"/"one" matches verb+pronoun phrases ("find me", "show me",
# "for one of the rooms"), so require an unambiguous solo-trip cue.
_SOLO_RE = re.compile(
    r"\b(?:just\s+me|just\s+myself|just\s+one|solo|"
    r"for\s+one(?:\s+person|\s+adult|\s+guest)?|"
    r"for\s+myself|by\s+myself|travelling\s+alone|traveling\s+alone)\b",
    re.I,
)

# Provider preference
_AIRBNB_RE = re.compile(r"\bairbnb\b", re.I)
_HOTEL_RE = re.compile(r"\bhotel\b", re.I)

# Location
_LOCATION_RE = re.compile(
    r"\b(?:in|at|near|around|to)\s+"
    r"([a-z][\w' .-]+?)"
    r"(?=\s+(?:for|on|next|this|tomorrow|tonight|over|with|from|between|"
    r"starting|on\s+the|to\s+stay)|"
    r"[.,?!]|$)", re.I,
)


# ── Public API ───────────────────────────────────────────────────────


def parse_location(text: str) -> str:
    """Extract city/area noun-phrase. Empty string means "couldn't find one"."""
    m = _LOCATION_RE.search(text or "")
    if not m:
        return ""
    return m.group(1).strip().rstrip(".,?!").title()


def parse_guests(text: str) -> int:
    """Return guest count; defaults to 2 when nothing matches."""
    text_l = (text or "").lower()
    # Couple patterns first — "me and my partner" otherwise gets misread as solo.
    if _COUPLE_RE.search(text_l):
        return 2
    if _SOLO_RE.search(text_l):
        return 1
    m = _FAMILY_RE.search(text_l) or _GUEST_NUM_RE.search(text_l)
    if m:
        raw = m.group(1).strip().lower()
        if raw.isdigit():
            n = int(raw)
        else:
            n = _WORD_NUMS.get(raw, 2)
        return max(1, min(n, 16))  # clamp to sane range
    return 2


def parse_provider_preference(text: str) -> list[str]:
    """Empty list means "fan out to all configured providers"."""
    text_l = (text or "").lower()
    airbnb_match = bool(_AIRBNB_RE.search(text_l))
    hotel_match = bool(_HOTEL_RE.search(text_l))
    if airbnb_match and not hotel_match:
        return ["apify_airbnb"]
    if hotel_match and not airbnb_match:
        return ["liteapi"]
    return []


def parse_dates(text: str, today: date | None = None) -> tuple[date, date]:
    """Return (check_in, check_out). Falls back to (today+7, today+9) when
    nothing in `text` looks like a date.

    Priority order:
    1. Explicit date range ("June 5 to 8", "from June 5 to June 8")
    2. Nights-from-anchor ("for 3 nights" + an anchor like "starting Friday")
    3. Bare nights count ("for a week", "for 3 nights") — starts week-out
    4. Named weekday ("next Monday")
    5. "tonight" / "tomorrow" / "next week"
    6. "weekend" — next Friday → Sunday
    7. Fallback: today + 7 days for 2 nights
    """
    today = today or date.today()
    text_l = (text or "").lower()

    # 1. Explicit date range
    rng = _DATE_RANGE_RE.search(text_l)
    if rng:
        result = _resolve_date_range(rng, today)
        if result is not None:
            return result

    # 2 + 3. Nights count
    nights = _parse_nights(text_l)

    # 4. Named weekday anchor
    weekday = _next_named_weekday(text_l, today)
    if weekday is not None:
        check_in = weekday
        check_out = check_in + timedelta(days=nights or 2)
        return check_in, check_out

    # 5. tonight / tomorrow / next week
    if _TONIGHT_RE.search(text_l):
        return today, today + timedelta(days=1)
    if _TOMORROW_RE.search(text_l):
        check_in = today + timedelta(days=1)
        return check_in, check_in + timedelta(days=nights or 1)
    if _NEXT_WEEK_RE.search(text_l):
        days_to_monday = (7 - today.weekday()) % 7 or 7
        check_in = today + timedelta(days=days_to_monday)
        return check_in, check_in + timedelta(days=nights or 2)

    # 6. Weekend
    if _WEEKEND_RE.search(text_l):
        days_to_fri = (4 - today.weekday()) % 7 or 7
        check_in = today + timedelta(days=days_to_fri)
        return check_in, check_in + timedelta(days=2)

    # 7. Fallback — a generic "find me somewhere" defaults to next-weekend-ish.
    check_in = today + timedelta(days=7)
    check_out = check_in + timedelta(days=nights or 2)
    return check_in, check_out


# ── Internal helpers ─────────────────────────────────────────────────


def _parse_nights(text_l: str) -> int:
    """Return the explicit nights count from "for N nights", or 0 if not stated."""
    m = _NIGHTS_RE.search(text_l)
    if not m:
        return 0
    raw_num = m.group(1).lower()
    unit = (m.group(2) or "").lower()
    if raw_num == "weekend":
        return 2
    if raw_num == "week" or unit.startswith("week"):
        return 7
    if raw_num == "night":
        return 1
    if raw_num.isdigit():
        n = int(raw_num)
    else:
        n = _WORD_NUMS.get(raw_num, 0)
    return max(0, min(n, 60))  # clamp


def _next_named_weekday(text_l: str, today: date) -> date | None:
    m = _WEEKDAY_RE.search(text_l)
    if not m:
        return None
    target = _WEEKDAYS[m.group(1).lower()]
    # "next monday" → at least 1 week away if today is monday; otherwise the
    # upcoming named weekday. Plain "monday" → same.
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0:
        # "this <weekday>" while it IS that weekday — use today; otherwise
        # voice users usually mean a week out, so push by 7.
        if "next" in m.group(0).lower():
            days_ahead = 7
    return today + timedelta(days=days_ahead)


def _resolve_date_range(match: re.Match, today: date) -> tuple[date, date] | None:
    """Best-effort to extract two dates from a captured month/day pair."""
    m1, d1, m1b, m2, d2, m2b = (match.group(i) for i in range(1, 7))
    month1 = _MONTHS.get((m1 or m1b or "").lower())
    month2 = _MONTHS.get((m2 or m2b or "").lower()) or month1
    if month1 is None or month2 is None:
        return None
    try:
        day1, day2 = int(d1), int(d2)
    except (TypeError, ValueError):
        return None

    # Year inference: pick the next occurrence of the start date in the future.
    candidate_year = today.year
    try:
        start = date(candidate_year, month1, day1)
    except ValueError:
        return None
    if start < today:
        candidate_year += 1
        try:
            start = date(candidate_year, month1, day1)
        except ValueError:
            return None
    # End year: same as start unless month2 < month1 (wraps year).
    end_year = candidate_year if month2 >= month1 else candidate_year + 1
    try:
        end = date(end_year, month2, day2)
    except ValueError:
        return None
    if end <= start:
        # User probably meant "to <day> of <same month next month>", or
        # they typoed. Default to start + 2 nights.
        end = start + timedelta(days=2)
    return start, end
