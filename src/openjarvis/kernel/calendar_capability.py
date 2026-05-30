"""Calendar capability — deterministic, never disavows, never lies.

This is the capability that the failing conversation exercised. The user asked
"any meetings tomorrow on my Outlook calendar"; the server fetched the data,
then the LLM said "I don't have the tools to access your Outlook calendar."

Here that is impossible:
  * intent detection is reused from ``intent_preexec`` (battle-tested regexes);
  * the tool is run with the CORRECT parameters per provider (the old code
    passed ``start/end`` to the Google tool, which only accepts
    ``time_min/time_max`` — so "tomorrow" was silently ignored);
  * ``ToolResult.success`` is honoured — a failed fetch becomes a spoken ERROR,
    never a fake "you have no meetings";
  * the spoken reply is built from the parsed events, not by an LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Tuple

from openjarvis.kernel.contracts import CapabilitySpec, Outcome

logger = logging.getLogger("openjarvis.kernel.calendar")

NAME = "calendar"


# ── Availability ─────────────────────────────────────────────────────────

def _outlook_available() -> bool:
    import os
    # Outlook/Graph is wired when a refresh token / client creds exist.
    return bool(
        os.environ.get("OUTLOOK_REFRESH_TOKEN", "").strip()
        or os.environ.get("MS_GRAPH_REFRESH_TOKEN", "").strip()
        or os.environ.get("OUTLOOK_CLIENT_ID", "").strip()
    )


def _google_available() -> bool:
    import os
    return bool(
        os.environ.get("GOOGLE_CALENDAR_TOKEN", "").strip()
        or os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
        or os.environ.get("GCAL_REFRESH_TOKEN", "").strip()
    )


def spec() -> CapabilitySpec:
    available = _outlook_available() or _google_available()
    which = []
    if _outlook_available():
        which.append("Outlook/Hotmail")
    if _google_available():
        which.append("Google")
    detail = (
        f"Connected calendars: {', '.join(which)}." if which
        else "No calendar account is OAuth-authorised yet."
    )
    return CapabilitySpec(
        name=NAME,
        summary="Check the user's calendar / meetings / appointments for a day or week.",
        available=available,
        detail=detail,
    )


# ── Intent detection (reuse intent_preexec) ───────────────────────────────

def detect(text: str) -> Optional[dict]:
    """Return {provider, start, end, window_label} or None. Uses the existing
    regexes so behaviour matches what shipped, but the kernel owns execution."""
    try:
        from openjarvis.server.intent_preexec import _detect_calendar_intent
    except Exception:  # pragma: no cover - defensive
        return None
    return _detect_calendar_intent(text or "")


# ── Tool execution (success-preserving) ───────────────────────────────────

def _run_tool(tool_name: str, **params: Any) -> Tuple[bool, str]:
    """Execute a registry tool. Returns (success, content). Any exception is a
    failure, not a silent empty result."""
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get(tool_name)
        if cls is None:
            return False, f"{tool_name} is not registered"
        result = cls().execute(**params)
        if result is None:
            return False, f"{tool_name} returned nothing"
        success = bool(getattr(result, "success", True))
        content = getattr(result, "content", None)
        if content is None:
            content = str(result)
        return success, content
    except Exception as exc:  # noqa: BLE001
        logger.warning("kernel.calendar tool %s raised: %s", tool_name, exc)
        return False, str(exc)


# ── Result parsing ─────────────────────────────────────────────────────────

def _strip_note_prefix(content: str) -> str:
    """The Google tool may prepend a ``[NOTE: ...]`` line before the JSON. Drop
    any leading bracketed note(s) so ``json.loads`` succeeds."""
    text = (content or "").lstrip()
    while text.startswith("["):
        # A JSON array also starts with '[' — only strip when it's a NOTE line.
        newline = text.find("\n")
        first_line = text[: newline if newline != -1 else len(text)]
        if "NOTE" in first_line.upper() and newline != -1:
            text = text[newline + 1:].lstrip()
        else:
            break
    return text


def _event_start_str(start: Any) -> str:
    """Best-effort human time from a Graph/Google start block."""
    if isinstance(start, dict):
        raw = start.get("dateTime") or start.get("date") or ""
    else:
        raw = str(start or "")
    raw = str(raw)
    # 2026-05-30T14:30:00 -> 14:30 ; date-only -> "all day"
    if "T" in raw:
        try:
            time_part = raw.split("T", 1)[1]
            hhmm = time_part[:5]
            return hhmm
        except Exception:  # noqa: BLE001
            return ""
    return ""


def parse_events(content: str) -> List[dict]:
    """Normalise Outlook (Graph) and Google calendar JSON to a list of
    ``{title, time}`` dicts. Returns [] for an empty calendar."""
    data = _strip_note_prefix(content)
    try:
        obj = json.loads(data)
    except Exception:
        return []
    items = []
    if isinstance(obj, dict):
        items = obj.get("value") or obj.get("items") or []
    elif isinstance(obj, list):
        items = obj
    events: List[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (
            it.get("subject")
            or it.get("summary")
            or it.get("title")
            or "untitled"
        )
        events.append({"title": str(title), "time": _event_start_str(it.get("start"))})
    return events


# ── Spoken-reply synthesis (deterministic, butler tone) ────────────────────

def _phrase(events: List[dict], window_label: str) -> str:
    n = len(events)
    if n == 0:
        return f"You have no meetings on your calendar {window_label}, sir."
    head = events[:3]
    parts = []
    for e in head:
        if e["time"]:
            parts.append(f"{e['title']} at {e['time']}")
        else:
            parts.append(e["title"])
    listing = "; ".join(parts)
    if n == 1:
        return f"You have one meeting {window_label}, sir: {listing}."
    if n <= 3:
        return f"You have {n} meetings {window_label}, sir: {listing}."
    return (
        f"You have {n} meetings {window_label}, sir. The first few: {listing}, "
        f"and {n - 3} more."
    )


def resolve(text: str) -> Outcome:
    """Top-level: detect → fetch → honour success → speak. PASSTHROUGH when this
    is not a calendar turn."""
    intent = detect(text)
    if not intent:
        return Outcome.passthrough()

    provider = intent["provider"]
    window_label = intent.get("window_label", "today")
    start = intent["start"]
    end = intent["end"]

    if provider == "outlook":
        if not _outlook_available() and _google_available():
            provider = "google"  # graceful fall-over to the connected account

    if provider == "outlook":
        success, content = _run_tool(
            "outlook_list_events", start=start, end=end, top=25
        )
    else:
        # Google's tool takes time_min/time_max — the OLD code passed start/end
        # here, so the window was silently ignored. Fixed.
        success, content = _run_tool(
            "calendar_list_events", time_min=start, time_max=end, max_results=50
        )

    if not success:
        logger.info("kernel.calendar fetch failed provider=%s: %s", provider, content[:160])
        which = "Outlook" if provider == "outlook" else "Google Calendar"
        return Outcome.error(
            f"I couldn't reach your {which} just now, sir — the connection "
            f"returned an error. Shall I try again in a moment?",
            capability=NAME,
            provider=provider,
        )

    events = parse_events(content)
    message = _phrase(events, window_label)
    status_ok = Outcome.ok if events else Outcome.empty
    return status_ok(
        message,
        capability=NAME,
        provider=provider,
        window=window_label,
        count=len(events),
        events=events,
    )
