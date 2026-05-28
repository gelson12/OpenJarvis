"""Server-side pre-execution for common email/calendar intents.

THE PROBLEM
-----------
Even with tools auto-injected and the system prompt nudging tool use,
LLMs (Gemini, Llama, Claude alike) still routinely choose to NARRATE
intent ("I'll check your calendar for today's meetings") instead of
emitting a real function_call. Result: user gets fabricated or
"I don't have that capability" replies.

THE FIX
-------
For UNAMBIGUOUS intent patterns (the user asked exactly for X data), we
detect the intent via regex, execute the tool ourselves, and inject the
result as a `tool` message in the conversation. The LLM then sees the
real data and just has to summarise — no tool-calling discretion involved.

Intents covered (additive — extend over time):
  - "check my calendar [today|tomorrow|this week|next week]"
  - "do I have any meetings [today|tomorrow|this week]"
  - "any emails from <name>"
  - "check my [outlook|gmail] inbox"
  - "unread emails"

Public API:
    maybe_preexecute(messages) -> Optional[(tool_name, result_text)]

If a match fires, the caller (chat_completions) appends a synthetic
tool/system message with the result before dispatching to the LLM.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time-window resolver
# ---------------------------------------------------------------------------

def _today_window() -> Tuple[str, str]:
    """Return (start, end) ISO-8601 UTC strings for today."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _tomorrow_window() -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _today_tomorrow_window() -> Tuple[str, str]:
    """Span today + tomorrow as a single window."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _this_week_window() -> Tuple[str, str]:
    """Mon 00:00 through Sun 23:59 in UTC."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _next_week_window() -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=now.weekday()) + timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _recent_past_window(days: int = 30) -> Tuple[str, str]:
    """Look-back window for 'last meeting' / 'recent' / 'previous' queries.
    Defaults to the last 30 days ending at the current moment."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return start.isoformat(), now.isoformat()


def _last_week_window() -> Tuple[str, str]:
    """Monday 00:00 through Sunday 23:59 of the previous week (UTC)."""
    now = datetime.now(timezone.utc)
    start_this_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    start = start_this_week - timedelta(days=7)
    end = start_this_week - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _day_window(target: datetime) -> Tuple[str, str]:
    """24-hour window for an arbitrary date (UTC)."""
    start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


# Round 19.1 — custom date parser. Production transcript showed the LLM
# disavowing when users asked about specific dates ("day after tomorrow",
# "May 30", "the 30th of June"). The tools fully support arbitrary
# time_min/time_max — _resolve_window just didn't know how to translate
# those phrasings. These regexes + helper extract a date target from a
# natural utterance.

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# "May 30", "May 30th", "30th of May", "30 May", "30 May 2026"
_DATE_PHRASE_RE = re.compile(
    r"\b("
    # "May 30 [2026]" / "May 30th [2026]"
    r"(?:january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|"
    r"august|aug|september|sept?|october|oct|november|nov|december|dec)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?(?:[,\s]+\d{4})?"
    r"|"
    # "30th of May [2026]" / "30 May [2026]"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(?:january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|"
    r"august|aug|september|sept?|october|oct|november|nov|december|dec)"
    r"(?:[,\s]+\d{4})?"
    r")\b",
    re.IGNORECASE,
)

# Relative day offsets
_DAY_AFTER_TOMORROW_RE = re.compile(
    r"\b(?:day\s+after\s+tomorrow|two\s+days\s+from\s+(?:now|today)|"
    r"in\s+two\s+days)\b", re.I,
)
_DAY_BEFORE_YESTERDAY_RE = re.compile(
    r"\b(?:day\s+before\s+yesterday|two\s+days\s+ago)\b", re.I,
)
# "next Monday" / "this Friday" — relative weekday
_RELATIVE_WEEKDAY_RE = re.compile(
    r"\b(?:(this|next|coming|last|past)\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tues?|wed|thu(?:r|rs)?|fri|sat|sun)\b",
    re.I,
)


def _parse_date_phrase(text: str) -> Optional[datetime]:
    """Try to extract an explicit date from the message. Returns a
    timezone-aware datetime at midnight UTC, or None on miss."""
    if not text:
        return None
    now = datetime.now(timezone.utc)

    # Day-relative: "day after tomorrow" / "day before yesterday"
    if _DAY_AFTER_TOMORROW_RE.search(text):
        return (now + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    if _DAY_BEFORE_YESTERDAY_RE.search(text):
        return (now - timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

    # Month + day: "May 30", "30 May", "30th of May", optional year
    m = _DATE_PHRASE_RE.search(text)
    if m:
        phrase = m.group(0).lower()
        # Extract month + day + optional year via flexible re
        date_part = re.search(
            r"(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
            r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|"
            r"july|jul|august|aug|september|sept?|october|oct|november|nov|"
            r"december|dec))"
            r"|"
            r"(?:(january|jan|february|feb|march|mar|april|apr|may|june|jun|"
            r"july|jul|august|aug|september|sept?|october|oct|november|nov|"
            r"december|dec)\s+(\d{1,2})(?:st|nd|rd|th)?)",
            phrase, re.I,
        )
        if date_part:
            if date_part.group(1):
                day = int(date_part.group(1))
                month = _MONTHS[date_part.group(2).lower()]
            else:
                month = _MONTHS[date_part.group(3).lower()]
                day = int(date_part.group(4))
            year_match = re.search(r"\b(20\d{2})\b", phrase)
            year = int(year_match.group(1)) if year_match else now.year
            # If parsed date is more than 60 days in the past, assume
            # the user meant next year (e.g. on Dec 20 saying "Jan 15"
            # means next January).
            try:
                target = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
                if (now - target).days > 60:
                    target = target.replace(year=year + 1)
                return target
            except ValueError:
                # Invalid date (e.g. June 31). Return None so the
                # caller knows; the LLM can handle the error.
                logger.info(
                    "date parse: invalid date %s-%s-%s in %r",
                    year, month, day, text[:60],
                )
                return None

    # Relative weekday: "next Monday" / "this Friday"
    wm = _RELATIVE_WEEKDAY_RE.search(text)
    if wm:
        qualifier = (wm.group(1) or "").lower()
        weekday_name = wm.group(2).lower()
        target_weekday = _WEEKDAYS.get(weekday_name)
        if target_weekday is not None:
            today_weekday = now.weekday()
            days_ahead = (target_weekday - today_weekday) % 7
            if qualifier == "next":
                days_ahead = days_ahead if days_ahead != 0 else 7
                days_ahead += (0 if days_ahead >= 7 else 7) if days_ahead < 7 else 0
                # Simpler: next monday is always >=7 days away (or 7 if today is monday)
                days_ahead = ((target_weekday - today_weekday) % 7) or 7
                if days_ahead < 7:
                    days_ahead += 7
            elif qualifier in ("last", "past"):
                days_ahead = -((today_weekday - target_weekday) % 7 or 7)
            else:
                # "this monday" or bare "monday" — next occurrence (or
                # today if today is monday)
                pass
            return (now + timedelta(days=days_ahead)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
    return None


_PAST_LANG_RE = re.compile(
    r"\b("
    r"last\s+(?:meeting|appointment|event|call|interview)|"
    r"previous\s+(?:meeting|appointment|event|call|interview)|"
    r"most\s+recent\s+(?:meeting|appointment|event|call|interview)|"
    r"recent\s+(?:meeting|appointment|event|call|interview|meetings|appointments)|"
    # bare temporal-past adjectives
    r"past\s+(?:meeting|meetings|week|month|days|appointments)|"
    # explicit "when was the last"
    r"when\s+(?:was|did)|"
    r"what\s+(?:was|were)\s+(?:my|the)\s+(?:last|previous|most\s+recent|recent)|"
    # "had a meeting" / "i had" past tense
    r"i\s+had\s+(?:a\s+)?(?:meeting|appointment|event|call)|"
    # "previously" / "earlier this week"
    r"previously|earlier\s+(?:this|last)\s+(?:week|month)"
    r")\b",
    re.IGNORECASE,
)


def _is_past_query(text: str) -> bool:
    """True when the user's phrasing clearly asks about PAST calendar
    events (not today/tomorrow/future). Used to choose between the
    look-back window and the default today window."""
    return bool(_PAST_LANG_RE.search(text or ""))


def _resolve_window(text: str) -> Optional[Tuple[str, str, str]]:
    """Detect the date window mentioned in `text`. Returns (label, start, end)
    or None if no recognised window."""
    t = text.lower()
    # Round 19.1 — explicit/custom date BEFORE the today/tomorrow checks
    # so "next Friday" / "May 30" / "day after tomorrow" don't get
    # silently swallowed by a stray "today" elsewhere in the sentence.
    custom = _parse_date_phrase(text)
    if custom is not None:
        s, e = _day_window(custom)
        # Friendly label: "Friday May 30" or just "Monday"
        label = custom.strftime("%A, %B %d")
        return (label, s, e)
    # Past-tense / recent-history queries take precedence over other
    # anchors because "last meeting" / "most recent" override default-today.
    if _is_past_query(t):
        # "last week" -> Mon..Sun of previous calendar week; everything
        # else falls back to a 30-day look-back ending now.
        if re.search(r"\blast\s+week\b", t):
            s, e = _last_week_window()
            return ("last week", s, e)
        s, e = _recent_past_window(30)
        return ("the last 30 days", s, e)
    if re.search(r"\btoday\b.*\btomorrow\b|\btomorrow\b.*\btoday\b", t):
        s, e = _today_tomorrow_window()
        return ("today and tomorrow", s, e)
    if re.search(r"\btomorrow\b", t):
        s, e = _tomorrow_window()
        return ("tomorrow", s, e)
    if re.search(r"\btoday\b|\bthis (?:morning|afternoon|evening)\b|\btonight\b", t):
        s, e = _today_window()
        return ("today", s, e)
    if re.search(r"\b(?:this|next)\s+week\b", t):
        if "next" in t:
            s, e = _next_week_window()
            return ("next week", s, e)
        s, e = _this_week_window()
        return ("this week", s, e)
    return None


# ---------------------------------------------------------------------------
# Intent detection — calendar / meetings
# ---------------------------------------------------------------------------

_CALENDAR_INTENT_RE = re.compile(
    r"\b("
    # any verb that could possibly mean "look it up"
    r"(?:check|verify|confirm|show|tell|see|list|read|find|get|"
    r"look(?:\s+up)?|access|retrieve|pull|fetch|review|inspect|view|"
    r"open|do\s+i\s+have|have\s+i\s+got|any|some|what|when|which|who)\s+"
    r"(?:me\s+|out\s+|for\s+|up\s+)?"
    r"(?:my\s+|the\s+|a\s+|an\s+)?(?:outlook\s+|google\s+|work\s+|"
    r"personal\s+)?"
    r"(?:calendar|meetings?|appointments?|schedule|agenda|events?|"
    r"bookings?|reservations?)|"
    r"(?:any|some|my)\s+(?:meetings?|appointments?|events?)|"
    r"what['\s]?s?\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule|agenda)|"
    # bare nouns near time anchors — "calendar today", "meetings tomorrow"
    r"(?:calendar|meetings?|events?|appointments?|schedule|agenda)\s+"
    r"(?:today|tomorrow|this\s+week|next\s+week|now)"
    r")\b",
    re.IGNORECASE,
)

# Sentence-level fallback: catches phrasings where the verb and the
# calendar noun are far apart, e.g.
#   "look into my outlook account and see if I have any scheduled for
#    tomorrow in the calendar"
# Requires: a calendar-domain noun AND (a query intent signal OR a
# time anchor). The intent signal is broad — query verbs, possessive
# "my", or auxiliary "do/have/any/anything".
_CALENDAR_NOUN_RE = re.compile(
    r"\b(calendar|meetings?|appointments?|schedule[d]?|agenda|"
    r"events?|bookings?|reservations?)\b",
    re.IGNORECASE,
)
_CALENDAR_SIGNAL_RE = re.compile(
    r"\b("
    # query verbs (whole-word, ensure context)
    r"check|verify|confirm|show|tell|see|list|read|find|fetch|"
    r"look\s+(?:up|into|at)|"
    r"access|retrieve|pull|review|inspect|view|"
    # explicit question + possessive patterns ("do I have", "is there")
    r"do\s+i\s+have|did\s+i\s+have|have\s+i\s+got|am\s+i|"
    r"is\s+there|are\s+there|"
    # question words paired with calendar
    r"what(?:'s|\s+is|\s+was|\s+were)?\s+(?:on|in|scheduled|my|the|"
    r"last|previous|most\s+recent|recent)|"
    r"when\s+(?:is|was|did)|which\s+(?:day|time)|"
    # strong time anchors (any of these next to a calendar noun is decisive)
    r"today|tomorrow|tonight|"
    r"this\s+(?:morning|afternoon|evening|week)|"
    r"next\s+(?:week|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|"
    # past-tense discriminators — let "last meeting" / "most recent
    # appointment" / "previous event" trip the calendar broad match
    r"last\s+(?:meeting|appointment|event|call|interview|week|month)|"
    r"previous\s+(?:meeting|appointment|event|call|interview|week|month)|"
    r"most\s+recent|recent\s+(?:meeting|meetings|appointment|"
    r"appointments|event|events)|"
    r"i\s+had\s+(?:a\s+)?(?:meeting|appointment|event|call)|"
    # "scheduled" as a discriminator — strong calendar signal even alone
    r"scheduled|booked"
    r")\b",
    re.IGNORECASE,
)


def _calendar_broad_match(text: str) -> bool:
    """Broad sentence-level calendar intent. True if the message contains
    a calendar noun AND any of: query verb, possessive, time anchor."""
    return bool(_CALENDAR_NOUN_RE.search(text) and _CALENDAR_SIGNAL_RE.search(text))

_PREFER_OUTLOOK_RE = re.compile(r"\b(outlook|hotmail|office\s*365|microsoft)\b", re.I)
_PREFER_GOOGLE_RE = re.compile(r"\b(google|gmail|gcal)\b", re.I)


def _detect_provider(
    text: str,
    *,
    history_texts: Optional[List[str]] = None,
    default: str = "outlook",
    google_label: str = "google",
) -> Tuple[str, bool]:
    """Resolve calendar/email provider with history fallback.

    Returns (provider, assumed) where `assumed=True` means the choice
    was the default (neither current message nor history specified a
    provider). Callers can surface this so the LLM knows to switch on
    a follow-up.

    Round 10.3: turn-detector fragmentation could land "Gmail" in one
    utterance and the noun ("calendar") in the next. We now scan up to
    the last 3 history user messages for provider hints before silently
    defaulting to Outlook.
    """
    if _PREFER_OUTLOOK_RE.search(text):
        return ("outlook", False)
    if _PREFER_GOOGLE_RE.search(text):
        return (google_label, False)
    # History sweep — most-recent 3 user messages
    if history_texts:
        for h in reversed(history_texts[-3:]):
            if not h:
                continue
            if _PREFER_GOOGLE_RE.search(h):
                return (google_label, False)
            if _PREFER_OUTLOOK_RE.search(h):
                return ("outlook", False)
    return (default, True)


def _detect_calendar_intent(
    text: str, *, history_texts: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Return {provider, start, end, window_label, provider_assumed} when
    the user asks for calendar data, else None."""
    if not text:
        return None
    # Narrow regex first (high confidence); fall back to broad sentence
    # match for phrasings where verb and calendar-noun are far apart.
    if not (_CALENDAR_INTENT_RE.search(text) or _calendar_broad_match(text)):
        return None
    window = _resolve_window(text)
    if window is None:
        # No explicit time window — default to today
        s, e = _today_window()
        window = ("today", s, e)
    label, start, end = window
    provider, assumed = _detect_provider(text, history_texts=history_texts)
    return {
        "provider": provider,
        "start": start,
        "end": end,
        "window_label": label,
        "provider_assumed": assumed,
    }


# ---------------------------------------------------------------------------
# Intent detection — email search
# ---------------------------------------------------------------------------

_EMAIL_SEARCH_INTENT_RE = re.compile(
    r"\b("
    # any plausible "look it up" verb
    r"(?:check|verify|confirm|show|tell|see|list|read|search|find|"
    r"get|look(?:\s+up)?|access|retrieve|pull|fetch|review|inspect|"
    r"view|open|any|some|what|when|which|who|do\s+i\s+have|"
    r"have\s+i\s+(?:got|received))\s+"
    r"(?:me\s+|for\s+|out\s+|up\s+|the\s+|my\s+)?"
    r"(?:last\s+|latest\s+|newest\s+|most\s+recent\s+|new\s+|recent\s+|"
    r"first\s+|today['\s]?s?\s+|yesterday['\s]?s?\s+)?"
    r"(?:my\s+|the\s+|a\s+|an\s+)?(?:outlook\s+|gmail\s+|hotmail\s+|"
    r"work\s+|personal\s+)?"
    r"(?:emails?|messages?|mail|inbox|notifications?)|"
    # "last/who sent/from <name>" phrasing
    r"(?:who(?:'s|\s+is|\s+was)?\s+the\s+(?:last|latest|most\s+recent)\s+"
    r"(?:person|sender|one))|"
    r"(?:emails?|messages?|mail)\s+from\s+\S+|"
    r"(?:from\s+\S+)\s+(?:emails?|messages?|mail)|"
    r"unread\s+(?:emails?|messages?|mail)|"
    # bare "any new mail" / "anything from X"
    r"any\s+(?:new\s+|recent\s+|unread\s+)?(?:emails?|mail|messages?)|"
    r"anything\s+from\s+\S+"
    r")\b",
    re.IGNORECASE,
)

_FROM_SENDER_RE = re.compile(
    r"(?:from|by|sent\s+by)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|\S+@\S+\.\S+)",
    re.IGNORECASE,
)

# Sentence-level fallback for email — same pattern as calendar:
# email-domain noun + any query/possessive/time signal anywhere.
_EMAIL_NOUN_RE = re.compile(
    r"\b(emails?|messages?|mail|inbox|notifications?)\b",
    re.IGNORECASE,
)


def _email_broad_match(text: str) -> bool:
    """Broad sentence-level email intent. True if the message contains
    an email noun AND a query/possessive/time signal (reusing the
    calendar signal regex — same intent vocabulary)."""
    return bool(_EMAIL_NOUN_RE.search(text) and _CALENDAR_SIGNAL_RE.search(text))


def _detect_email_intent(
    text: str, *, history_texts: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # Narrow regex first; fall back to broad sentence match.
    if not (_EMAIL_SEARCH_INTENT_RE.search(text) or _email_broad_match(text)):
        return None
    sender = None
    m = _FROM_SENDER_RE.search(text)
    if m:
        sender = m.group(1).strip()
    # Round 10.3 — history-aware provider with assumption flag.
    # For email the Google brand uses 'gmail' as the provider label.
    provider, assumed = _detect_provider(
        text, history_texts=history_texts, google_label="gmail",
    )
    is_unread = bool(re.search(r"\bunread\b", text, re.I))
    return {
        "provider": provider,
        "sender": sender,
        "is_unread": is_unread,
        "provider_assumed": assumed,
    }


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _run_outlook_list_events(start: str, end: str) -> Optional[str]:
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("outlook_list_events")
        if cls is None:
            return None
        result = cls().execute(start=start, end=end, top=25)
        if result is None:
            return None
        return getattr(result, "content", None) or str(result)
    except Exception as exc:
        logger.warning("intent_preexec: outlook_list_events failed: %s", exc)
        return None


def _run_calendar_list_events(start: str, end: str) -> Optional[str]:
    """List Google Calendar events. Round 11.2: tries the 'primary'
    account first; if its OAuth isn't configured (refresh token missing)
    we automatically retry with account='bridge' before giving up.

    Also fixes a pre-existing param-name bug: the tool spec uses
    `time_min`/`time_max`, not `start`/`end`.
    """
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("calendar_list_events")
        if cls is None:
            return None
        # First attempt — primary account
        result = cls().execute(time_min=start, time_max=end, account="primary")
        content = getattr(result, "content", None) or str(result)
        # Round 11.2 — auto-fallback to bridge account when primary OAuth
        # isn't configured. The error string contains "OAuth not
        # configured" or "GOOGLE_CLIENT_ID"-style markers.
        success = bool(getattr(result, "success", True))
        looks_like_oauth_missing = (
            ("oauth not configured" in (content or "").lower())
            or ("google_client_id" in (content or "").lower())
            or ("google_refresh_token" in (content or "").lower())
        )
        if (not success) or looks_like_oauth_missing:
            from openjarvis.core.env import is_configured
            if is_configured("BRIDGE_GOOGLE_REFRESH_TOKEN"):
                logger.info(
                    "intent_preexec.calendar: primary OAuth missing — "
                    "retrying with account='bridge'"
                )
                try:
                    result2 = cls().execute(
                        time_min=start, time_max=end, account="bridge",
                    )
                    content2 = getattr(result2, "content", None) or str(result2)
                    success2 = bool(getattr(result2, "success", True))
                    if success2 and content2 and "error" not in content2[:60].lower():
                        # Round 18.1 — original prefix said "Google
                        # account: BRIDGE" which the LLM mis-read as a
                        # tool name `google_bridge` and tried to call
                        # it 4-5 times before timing out (~5 min hang).
                        # Reworded to be a plain English note that
                        # contains nothing resembling a tool identifier.
                        return (
                            "[NOTE: returned data is from the user's "
                            "secondary Google account. Primary Google "
                            "is not OAuth-authorized; no action needed.]\n"
                            + content2
                        )
                    # Round 16.3 — bridge ALSO failed (e.g. expired
                    # refresh token returning 400). Surface that fact
                    # to the LLM so it tells the user what's actually
                    # broken instead of falsely claiming "Google isn't
                    # authorized" (it WAS authorized; the token expired).
                    logger.warning(
                        "intent_preexec.calendar bridge retry returned "
                        "error: %s", (content2 or "")[:200],
                    )
                    return (
                        "[Google Calendar UNAVAILABLE — both primary and "
                        "BRIDGE accounts failed]\n"
                        f"Primary error: {(content or '')[:200]}\n"
                        f"Bridge error: {(content2 or '')[:200]}\n"
                        "INSTRUCTION TO LLM: tell the user CONCISELY that "
                        "Google Calendar isn't reachable right now. If the "
                        "bridge error mentions a 400 Bad Request or token "
                        "refresh, suggest re-running the OAuth setup for "
                        "the bridge account. Do NOT say 'I don't have the "
                        "tool' — the tool exists, the credentials need "
                        "renewal."
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "intent_preexec.calendar bridge retry failed: %s", exc,
                    )
        return content
    except Exception as exc:
        logger.warning("intent_preexec: calendar_list_events failed: %s", exc)
        return None


def _run_outlook_list_messages(sender: Optional[str], is_unread: bool) -> Optional[str]:
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("outlook_list_messages")
        if cls is None:
            return None
        # Outlook's $filter syntax: contains(from/emailAddress/name,'...')
        filters = []
        if is_unread:
            filters.append("isRead eq false")
        if sender and "@" in sender:
            filters.append(f"from/emailAddress/address eq '{sender}'")
        elif sender:
            filters.append(f"contains(from/emailAddress/name,'{sender}')")
        kwargs: Dict[str, Any] = {"top": 10}
        if filters:
            kwargs["filter"] = " and ".join(filters)
        result = cls().execute(**kwargs)
        return getattr(result, "content", None) or str(result)
    except Exception as exc:
        logger.warning("intent_preexec: outlook_list_messages failed: %s", exc)
        return None


def _run_gmail_list_messages(sender: Optional[str], is_unread: bool) -> Optional[str]:
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("gmail_list_messages")
        if cls is None:
            return None
        parts = []
        if is_unread:
            parts.append("is:unread")
        if sender:
            parts.append(f"from:{sender}")
        q = " ".join(parts) if parts else "newer_than:1d"
        # First attempt — primary account
        result = cls().execute(q=q, account="primary")
        content = getattr(result, "content", None) or str(result)
        # Round 19.2 — mirror the calendar bridge-fallback pattern.
        # Without this, "check my Gmail" was disavowing with
        # "Google OAuth not configured" even though the BRIDGE account
        # is fully working (the user's primary refresh token was never
        # captured, but the BRIDGE one is live).
        success = bool(getattr(result, "success", True))
        looks_like_oauth_missing = (
            ("oauth not configured" in (content or "").lower())
            or ("google_client_id" in (content or "").lower())
            or ("google_refresh_token" in (content or "").lower())
        )
        if (not success) or looks_like_oauth_missing:
            from openjarvis.core.env import is_configured
            if is_configured("BRIDGE_GOOGLE_REFRESH_TOKEN"):
                logger.info(
                    "intent_preexec.gmail: primary OAuth missing — "
                    "retrying with account='bridge'"
                )
                try:
                    result2 = cls().execute(q=q, account="bridge")
                    content2 = getattr(result2, "content", None) or str(result2)
                    success2 = bool(getattr(result2, "success", True))
                    if success2 and content2 and "error" not in content2[:60].lower():
                        return (
                            "[NOTE: returned data is from the user's "
                            "secondary Google account. Primary Google "
                            "is not OAuth-authorized; no action needed.]\n"
                            + content2
                        )
                    logger.warning(
                        "intent_preexec.gmail bridge retry returned "
                        "error: %s", (content2 or "")[:200],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "intent_preexec.gmail bridge retry failed: %s", exc,
                    )
        return content
    except Exception as exc:
        logger.warning("intent_preexec: gmail_list_messages failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_INTENT_PREEXEC_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def maybe_preexecute(
    latest_user_text: str,
    *,
    history_texts: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """If the user's latest message matches a known intent, execute the
    corresponding tool and return a context block to inject into the LLM
    prompt. Returns None when no intent matches.

    `history_texts` is the optional list of prior user message contents
    (most recent first or last — both supported; the provider detector
    scans the tail). When provided, "Gmail" mentioned in a prior turn
    will route a subsequent ambiguous "any meetings today?" to Google
    instead of silently defaulting to Outlook (Round 10.3).

    Return shape:
        {
            "tool_name": "outlook_list_events",
            "result": "<JSON / text result>",
            "context_block": "[Pre-executed tool result follows...]"
        }
    """
    if not _enabled() or not latest_user_text:
        return None

    text = latest_user_text.strip()

    # Round 17.1 — fragment-tolerant intent text.
    # Voice STT routinely splits a single request into 2-4 separate user
    # turns ("do I have a meeting" / "schedule on my Google Calendar?" /
    # "Today."). If we run intent detection ONLY on the latest fragment
    # ("Today.") it returns None -> LLM disavows ("I don't have a tool
    # to check your calendar"). Defence in depth: when the current text
    # is short and recent history contains calendar/email keywords,
    # build a combined-text view (last 3 user turns joined) and use
    # THAT for intent regex matching. The actual tool call still uses
    # the combined view for things like provider/window detection.
    intent_text = text
    if (
        history_texts
        and len(text) < 40  # short -> likely a fragment continuation
        and not (_CALENDAR_INTENT_RE.search(text) or _calendar_broad_match(text))
        and not (_EMAIL_SEARCH_INTENT_RE.search(text) or _email_broad_match(text))
    ):
        recent = " ".join((history_texts or [])[-3:] + [text]).strip()
        if recent and recent != text:
            if (_CALENDAR_INTENT_RE.search(recent) or _calendar_broad_match(recent)
                or _EMAIL_SEARCH_INTENT_RE.search(recent) or _email_broad_match(recent)):
                logger.info(
                    "intent_preexec: using history-joined text (%d chars) "
                    "for fragment continuation. latest=%r",
                    len(recent), text[:40],
                )
                intent_text = recent

    # ── Self-improvement layer: check LEARNED intents first ─────────────
    # learned_intents.match_learned() returns the full match dict
    # ({domain, action, hint_text, regex}) when any auto-promoted pattern
    # (from prior disavowals across ANY domain) matches.
    #
    # This is how the loop closes universally (Round 8):
    #   * action="preexec" — the domain has a server-side tool executor
    #     (email/calendar/watch). Fire it directly.
    #   * action="prompt_hint" — the domain has no tool executor
    #     (history/science/code/math/…). Inject the learned hint as a
    #     system context block so the LLM picks the right fallback
    #     (web_search, knowledge_search, honest reasoning) instead of
    #     disavowing again.
    try:
        from openjarvis.server import learned_intents as _li
        _learned = _li.match_learned(text)
    except Exception:
        _learned = None

    if _learned and _learned.get("action") == "preexec":
        domain = _learned.get("domain")
        if domain == "calendar":
            cal = _detect_calendar_intent(text) or {
                "provider": "outlook" if not _PREFER_GOOGLE_RE.search(text) else "google",
                "start": _today_window()[0],
                "end": _today_window()[1],
                "window_label": "today",
            }
            if cal["provider"] == "outlook":
                tool_name = "outlook_list_events"
                result = _run_outlook_list_events(cal["start"], cal["end"])
            else:
                tool_name = "calendar_list_events"
                result = _run_calendar_list_events(cal["start"], cal["end"])
            if result:
                block = (
                    f"PRE-EXECUTED TOOL RESULT (via LEARNED intent, from "
                    f"{tool_name}, window: {cal['window_label']}):\n{result}\n\n"
                    "INSTRUCTION: This pattern was auto-promoted because past "
                    "failures matched it. Trust the data above and summarise."
                )
                logger.info("intent_preexec.LEARNED.calendar served via %s", tool_name)
                return {"tool_name": tool_name, "result": result, "context_block": block}

        elif domain == "email":
            provider = "outlook" if not _PREFER_GOOGLE_RE.search(text) else "gmail"
            if provider == "outlook":
                tool_name = "outlook_list_messages"
                result = _run_outlook_list_messages(sender=None, is_unread=False)
            else:
                tool_name = "gmail_list_messages"
                result = _run_gmail_list_messages(sender=None, is_unread=False)
            if result:
                block = (
                    f"PRE-EXECUTED TOOL RESULT (via LEARNED intent, from "
                    f"{tool_name}):\n{result}\n\n"
                    "INSTRUCTION: This pattern was auto-promoted from past "
                    "failures. Trust this real data — summarise in one sentence."
                )
                logger.info("intent_preexec.LEARNED.email served via %s", tool_name)
                return {"tool_name": tool_name, "result": result, "context_block": block}

        # 'watch' domain has its own scheduling path elsewhere; if/when
        # it grows a pre-executor here, add the elif branch above. For
        # now, fall through to hardcoded regex detection below.

    elif _learned and _learned.get("action") == "prompt_hint":
        hint_text = _learned.get("hint_text")
        if hint_text:
            block = (
                f"LEARNED PROMPT HINT (auto-promoted from past failures in "
                f"domain={_learned.get('domain')}):\n{hint_text}\n\n"
                "Follow this hint for the user's question below. Do NOT "
                "claim you can't help — use the suggested fallback path."
            )
            logger.info(
                "intent_preexec.LEARNED.prompt_hint domain=%s",
                _learned.get("domain"),
            )
            return {
                "tool_name": None,
                "result": None,
                "context_block": block,
            }

    # ── Independent lookup in the prompt-hint store ─────────────────────
    # learned_prompt_hints stores its hints separately from
    # learned_intents.json (so the daemon can populate it without touching
    # the patterns store). Check it too so we still inject the hint when
    # the user's phrasing matches a hint regex even if no learned-intent
    # entry was created.
    try:
        from openjarvis.server import learned_prompt_hints as _hints_mod
        _hint_hit = _hints_mod.lookup(text)
    except Exception:
        _hint_hit = None
    if _hint_hit:
        hint_text = _hint_hit.get("hint_text")
        if hint_text:
            block = (
                f"LEARNED PROMPT HINT (auto-promoted, domain="
                f"{_hint_hit.get('domain')}):\n{hint_text}\n\n"
                "Follow this hint instead of disavowing capability."
            )
            logger.info(
                "intent_preexec.HINT.matched domain=%s",
                _hint_hit.get("domain"),
            )
            return {
                "tool_name": None,
                "result": None,
                "context_block": block,
            }

    # Calendar (hardcoded regex path)
    # Round 17.1 — use the fragment-tolerant intent_text so detection
    # works even when STT split the request across multiple turns.
    cal = _detect_calendar_intent(intent_text, history_texts=history_texts)
    if cal:
        if cal["provider"] == "outlook":
            tool_name = "outlook_list_events"
            result = _run_outlook_list_events(cal["start"], cal["end"])
        else:
            tool_name = "calendar_list_events"
            result = _run_calendar_list_events(cal["start"], cal["end"])
        if result is None:
            logger.info(
                "intent_preexec: matched calendar intent but tool returned None"
            )
            return None

        # Round 11.3 — when Google OAuth is unconfigured AND bridge is
        # unavailable, the result still contains the raw error string.
        # The LLM has been hallucinating a `google_bridge` tool to "fix
        # it" — wasting 5+ tool-call retries before timing out. Replace
        # the raw error with a short, action-able context block that
        # gives the LLM a clear sentence to read aloud instead.
        looks_like_oauth_error = (
            "oauth not configured" in result.lower()
            or "google_client_id" in result.lower()
            or "google_refresh_token" in result.lower()
        )
        if cal["provider"] != "outlook" and looks_like_oauth_error:
            block = (
                "GOOGLE CALENDAR UNAVAILABLE: the user's Google account "
                "has not finished the one-time OAuth authorization for "
                "calendar access (the refresh token is missing).\n\n"
                "ABSOLUTE INSTRUCTIONS (you MUST follow these):\n"
                "  1. Tell the user PLAINLY in ONE sentence: \"Your Google "
                "Calendar isn't authorized yet, sir — I'd need you to "
                "complete the one-time OAuth setup. Your Outlook calendar "
                "is working if you'd like me to check that instead.\"\n"
                "  2. Do NOT invent tool names like `google_bridge` — no "
                "such tool exists. Do NOT call any tool to 'fix' this.\n"
                "  3. Do NOT loop or retry. Speak the sentence and stop.\n"
                "  4. If the user offers to switch to Outlook, just say "
                "yes and wait for their next request.\n"
            )
            logger.info("intent_preexec.calendar: surfaced OAuth-missing notice")
            return {"tool_name": tool_name, "result": result, "context_block": block}
        # Hotfix #2: when the user explicitly asked about past meetings,
        # nudge the LLM to surface the MOST RECENT entry rather than dump
        # the full list. The look-back tool result contains every event
        # in the window — the user wants the latest one.
        past_nudge = ""
        if _is_past_query(intent_text):
            past_nudge = (
                "  6. The user asked about a PAST meeting (e.g. 'last meeting',\n"
                "     'most recent'). Pick the SINGLE most-recent event from\n"
                "     the list above and answer with its date + subject.\n"
                "     Do not list every event unless asked.\n"
            )
        # Round 10.3 — surface the provider-assumption flag so the LLM
        # knows to switch on a follow-up if the user actually wanted the
        # other provider.
        assumption_note = ""
        if cal.get("provider_assumed"):
            assumption_note = (
                f"  7. PROVIDER ASSUMPTION: defaulted to {cal['provider']} "
                "because the user did not name a provider and there was no\n"
                "     hint in recent history. If the user follows up naming\n"
                "     Google/Gmail/Outlook, SWITCH and re-run the appropriate\n"
                "     calendar tool — do NOT say you can't.\n"
            )
        block = (
            f"PRE-EXECUTED TOOL RESULT (from {tool_name}, window: "
            f"{cal['window_label']}):\n{result}\n\n"
            "ABSOLUTE INSTRUCTIONS (you MUST follow these):\n"
            # Round 19.3 — anti-laziness. LLM was saying "you already
            # have this data from earlier" on every repeated query
            # because of history-context bleed. The pre-executor reruns
            # FRESH every turn — the result above IS fresh data.
            "  1. The result above is FRESH data retrieved THIS turn —\n"
            "     it is NOT 'data from earlier'. Even if you answered\n"
            "     a similar question in a previous turn, the pre-executor\n"
            "     just ran the tool AGAIN to get the current state. Treat\n"
            "     this as if it were the first time. Never say things\n"
            "     like 'you already have this' or 'I retrieved this\n"
            "     earlier' — that misleads the user.\n"
            "  2. Summarise these REAL results in one short sentence.\n"
            "  3. Do NOT call the tool again — it already ran.\n"
            "  4. Do NOT say 'I'll check' — you already have the data.\n"
            "  5. Do NOT apologise for or disavow this result. The tool was\n"
            "     actually invoked successfully against the user's real\n"
            "     Outlook/Calendar account just now. If the user asks a\n"
            "     follow-up like 'Today?' / 'really?' / 'are you sure?',\n"
            "     CONFIRM the data above is correct (you literally just\n"
            "     retrieved it). NEVER respond with 'I don't have a tool\n"
            "     for that' — you DO have it and you JUST used it.\n"
            "  6. If the result shows no events, say so honestly\n"
            "     ('No meetings on your calendar today, sir.').\n"
            # Round 19.1 — the window label may be a specific date like
            # "Friday, May 30" not just "today/tomorrow". Mention it.
            "  7. The window covered is shown above (e.g. 'today',\n"
            "     'tomorrow', or a specific date like 'Friday, May 30').\n"
            "     Mention it explicitly so the user knows which date you\n"
            "     just checked. You CAN handle custom dates — never say\n"
            "     'I can't check specific dates'.\n"
            # Round 18.2 — anti-hallucination for tool-name invention.
            "  8. Do NOT invent or call any function whose name was not\n"
            "     listed in your tool schema. In particular, names like\n"
            "     `google_bridge`, `outlook_bridge`, `calendar_bridge`\n"
            "     do NOT exist — ANY hint of a 'bridge' / 'secondary'\n"
            "     account in the result above is descriptive METADATA,\n"
            "     not a tool you should call. The data is already here;\n"
            "     just summarise it and stop.\n"
            f"{past_nudge}"
            f"{assumption_note}"
        )
        logger.info(
            "intent_preexec.calendar served via %s window=%s",
            tool_name, cal["window_label"],
        )
        return {"tool_name": tool_name, "result": result, "context_block": block}

    # Email
    eml = _detect_email_intent(intent_text, history_texts=history_texts)
    if eml:
        if eml["provider"] == "outlook":
            tool_name = "outlook_list_messages"
            result = _run_outlook_list_messages(eml["sender"], eml["is_unread"])
        else:
            tool_name = "gmail_list_messages"
            result = _run_gmail_list_messages(eml["sender"], eml["is_unread"])
        if result is None:
            logger.info(
                "intent_preexec: matched email intent but tool returned None"
            )
            return None
        sender_part = f" (sender filter: {eml['sender']})" if eml["sender"] else ""
        unread_part = " (unread only)" if eml["is_unread"] else ""
        # Round 10.3 — provider-assumption nudge for email too.
        assumption_note = ""
        if eml.get("provider_assumed"):
            assumption_note = (
                f"  6. PROVIDER ASSUMPTION: defaulted to {eml['provider']} "
                "because the user did not name a provider. If they follow up\n"
                "     naming Google/Gmail/Outlook, SWITCH and re-run the\n"
                "     appropriate email tool instead of disavowing.\n"
            )
        block = (
            f"PRE-EXECUTED TOOL RESULT (from {tool_name}{sender_part}"
            f"{unread_part}):\n{result}\n\n"
            "ABSOLUTE INSTRUCTIONS (you MUST follow these):\n"
            "  1. Summarise these REAL results in one short sentence.\n"
            "  2. Do NOT call the tool again — it already ran.\n"
            "  3. Do NOT apologise for or disavow this result — the tool\n"
            "     was actually invoked successfully just now.\n"
            "  4. If the user asks a follow-up like 'really?' / 'are you\n"
            "     sure?', CONFIRM the data above. NEVER backtrack with\n"
            "     'I don't have a tool for that' — you DO have it and\n"
            "     you just used it.\n"
            "  5. If the result is empty, say so honestly ('No matching\n"
            "     emails, sir.').\n"
            # Round 18.2 — same anti-hallucination guard as calendar.
            "  6. Do NOT invent or call functions named `google_bridge`,\n"
            "     `gmail_bridge`, `outlook_bridge`, etc. — they do NOT\n"
            "     exist. Any 'bridge' / 'secondary account' note in the\n"
            "     result is descriptive METADATA, not a tool to call.\n"
            f"{assumption_note}"
        )
        logger.info(
            "intent_preexec.email served via %s sender=%s unread=%s",
            tool_name, eml["sender"], eml["is_unread"],
        )
        return {"tool_name": tool_name, "result": result, "context_block": block}

    return None
