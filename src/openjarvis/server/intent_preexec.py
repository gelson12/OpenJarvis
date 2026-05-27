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


def _resolve_window(text: str) -> Optional[Tuple[str, str, str]]:
    """Detect the date window mentioned in `text`. Returns (label, start, end)
    or None if no recognised window."""
    t = text.lower()
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

_PREFER_OUTLOOK_RE = re.compile(r"\b(outlook|hotmail|office\s*365|microsoft)\b", re.I)
_PREFER_GOOGLE_RE = re.compile(r"\b(google|gmail|gcal)\b", re.I)


def _detect_calendar_intent(text: str) -> Optional[Dict[str, Any]]:
    """Return {provider, start, end, window_label} when the user asks for
    calendar data, else None."""
    if not text:
        return None
    if not _CALENDAR_INTENT_RE.search(text):
        return None
    window = _resolve_window(text)
    if window is None:
        # No explicit time window — default to today
        s, e = _today_window()
        window = ("today", s, e)
    label, start, end = window
    if _PREFER_OUTLOOK_RE.search(text):
        provider = "outlook"
    elif _PREFER_GOOGLE_RE.search(text):
        provider = "google"
    else:
        provider = "outlook"  # default
    return {
        "provider": provider,
        "start": start,
        "end": end,
        "window_label": label,
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


def _detect_email_intent(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    if not _EMAIL_SEARCH_INTENT_RE.search(text):
        return None
    sender = None
    m = _FROM_SENDER_RE.search(text)
    if m:
        sender = m.group(1).strip()
    if _PREFER_OUTLOOK_RE.search(text):
        provider = "outlook"
    elif _PREFER_GOOGLE_RE.search(text):
        provider = "gmail"
    else:
        provider = "outlook"
    is_unread = bool(re.search(r"\bunread\b", text, re.I))
    return {
        "provider": provider,
        "sender": sender,
        "is_unread": is_unread,
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
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("calendar_list_events")
        if cls is None:
            return None
        result = cls().execute(start=start, end=end)
        return getattr(result, "content", None) or str(result)
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
        result = cls().execute(q=q)
        return getattr(result, "content", None) or str(result)
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


def maybe_preexecute(latest_user_text: str) -> Optional[Dict[str, Any]]:
    """If the user's latest message matches a known intent, execute the
    corresponding tool and return a context block to inject into the LLM
    prompt. Returns None when no intent matches.

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

    # ── Self-improvement layer: check LEARNED intents first ─────────────
    # learned_intents.match_category() returns the category if any auto-
    # promoted pattern (from prior disavowals) matches. This is how the
    # loop closes: failures yesterday → patterns auto-promoted overnight →
    # today they bypass the LLM entirely.
    try:
        from openjarvis.server import learned_intents as _li
        _learned_cat = _li.match_category(text)
    except Exception:
        _learned_cat = None

    if _learned_cat == "calendar":
        # Force calendar pre-execution regardless of hardcoded regex
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

    if _learned_cat == "email":
        # Default to outlook + recent
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

    # Calendar (hardcoded regex path)
    cal = _detect_calendar_intent(text)
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
        block = (
            f"PRE-EXECUTED TOOL RESULT (from {tool_name}, window: "
            f"{cal['window_label']}):\n{result}\n\n"
            "ABSOLUTE INSTRUCTIONS (you MUST follow these):\n"
            "  1. Summarise these REAL results in one short sentence.\n"
            "  2. Do NOT call the tool again — it already ran.\n"
            "  3. Do NOT say 'I'll check' — you already have the data.\n"
            "  4. Do NOT apologise for or disavow this result. The tool was\n"
            "     actually invoked successfully against the user's real\n"
            "     Outlook/Calendar account just now. If the user asks a\n"
            "     follow-up like 'Today?' / 'really?' / 'are you sure?',\n"
            "     CONFIRM the data above is correct (you literally just\n"
            "     retrieved it). NEVER respond with 'I don't have a tool\n"
            "     for that' — you DO have it and you JUST used it.\n"
            "  5. If the result shows no events, say so honestly\n"
            "     ('No meetings on your calendar today, sir.')."
        )
        logger.info(
            "intent_preexec.calendar served via %s window=%s",
            tool_name, cal["window_label"],
        )
        return {"tool_name": tool_name, "result": result, "context_block": block}

    # Email
    eml = _detect_email_intent(text)
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
            "     emails, sir.')."
        )
        logger.info(
            "intent_preexec.email served via %s sender=%s unread=%s",
            tool_name, eml["sender"], eml["is_unread"],
        )
        return {"tool_name": tool_name, "result": result, "context_block": block}

    return None
