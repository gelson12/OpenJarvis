"""Email capability — deterministic inbox lookups.

Same contract as the calendar capability: detect → fetch (success-preserving)
→ speak from real data. A failed fetch is an honest ERROR, never a fake
"no emails".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, List, Optional, Tuple

from openjarvis.kernel.contracts import CapabilitySpec, Outcome

logger = logging.getLogger("openjarvis.kernel.email")

NAME = "email"


def _outlook_available() -> bool:
    return bool(
        os.environ.get("OUTLOOK_REFRESH_TOKEN", "").strip()
        or os.environ.get("MS_GRAPH_REFRESH_TOKEN", "").strip()
        or os.environ.get("OUTLOOK_CLIENT_ID", "").strip()
    )


def _gmail_available() -> bool:
    return bool(
        os.environ.get("GMAIL_TOKEN", "").strip()
        or os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
        or os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
    )


def spec() -> CapabilitySpec:
    which = []
    if _outlook_available():
        which.append("Outlook/Hotmail")
    if _gmail_available():
        which.append("Gmail")
    return CapabilitySpec(
        name=NAME,
        summary="Search the user's email (Outlook / Gmail): unread, recent, or from a sender.",
        available=bool(which),
        detail=(f"Connected mailboxes: {', '.join(which)}." if which
                else "No mailbox is OAuth-authorised yet."),
    )


def detect(text: str) -> Optional[dict]:
    try:
        from openjarvis.server.intent_preexec import _detect_email_intent
    except Exception:  # pragma: no cover
        return None
    return _detect_email_intent(text or "")


def _run_tool(tool_name: str, **params: Any) -> Tuple[bool, str]:
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get(tool_name)
        if cls is None:
            return False, f"{tool_name} is not registered"
        result = cls().execute(**params)
        if result is None:
            return False, f"{tool_name} returned nothing"
        return bool(getattr(result, "success", True)), (
            getattr(result, "content", None) or str(result)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("kernel.email tool %s raised: %s", tool_name, exc)
        return False, str(exc)


def _strip_note_prefix(content: str) -> str:
    text = (content or "").lstrip()
    while text.startswith("["):
        newline = text.find("\n")
        first_line = text[: newline if newline != -1 else len(text)]
        if "NOTE" in first_line.upper() and newline != -1:
            text = text[newline + 1:].lstrip()
        else:
            break
    return text


def parse_messages(content: str) -> List[dict]:
    """Normalise Outlook (Graph) + Gmail JSON to ``{sender, subject}`` dicts."""
    try:
        obj = json.loads(_strip_note_prefix(content))
    except Exception:
        return []
    items = []
    if isinstance(obj, dict):
        items = obj.get("value") or obj.get("messages") or obj.get("items") or []
    elif isinstance(obj, list):
        items = obj
    out: List[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # Outlook: from.emailAddress.name ; Gmail: payload headers / 'from'
        sender = ""
        frm = it.get("from")
        if isinstance(frm, dict):
            ea = frm.get("emailAddress") or {}
            sender = ea.get("name") or ea.get("address") or ""
        elif isinstance(frm, str):
            sender = frm
        sender = sender or it.get("sender") or "someone"
        subject = it.get("subject") or it.get("snippet") or "no subject"
        out.append({"sender": str(sender), "subject": str(subject)})
    return out


def _phrase(msgs: List[dict], sender: Optional[str], unread: bool) -> str:
    qualifier = "unread " if unread else ""
    where = f" from {sender}" if sender else ""
    n = len(msgs)
    if n == 0:
        return f"No {qualifier}emails{where}, sir."
    head = msgs[:3]
    listing = "; ".join(f"{m['sender']} — {m['subject']}" for m in head)
    if n == 1:
        return f"One {qualifier}email{where}, sir: {listing}."
    if n <= 3:
        return f"{n} {qualifier}emails{where}, sir: {listing}."
    return f"{n} {qualifier}emails{where}, sir. Most recent: {listing}."


def resolve(text: str) -> Outcome:
    intent = detect(text)
    if not intent:
        return Outcome.passthrough()
    provider = intent["provider"]
    sender = intent.get("sender")
    unread = bool(intent.get("is_unread"))

    if provider == "outlook" and not _outlook_available() and _gmail_available():
        provider = "gmail"

    if provider == "outlook":
        filters = []
        if unread:
            filters.append("isRead eq false")
        if sender and "@" in sender:
            filters.append(f"from/emailAddress/address eq '{sender}'")
        elif sender:
            filters.append(f"contains(from/emailAddress/name,'{sender}')")
        kwargs: dict = {"top": 10}
        if filters:
            kwargs["filter"] = " and ".join(filters)
        success, content = _run_tool("outlook_list_messages", **kwargs)
    else:
        parts = []
        if unread:
            parts.append("is:unread")
        if sender:
            parts.append(f"from:{sender}")
        q = " ".join(parts) if parts else "newer_than:1d"
        success, content = _run_tool("gmail_list_messages", q=q)

    if not success:
        which = "Outlook" if provider == "outlook" else "Gmail"
        return Outcome.error(
            f"I couldn't reach your {which} just now, sir — it returned an "
            f"error. Shall I try again?",
            capability=NAME,
            provider=provider,
        )

    msgs = parse_messages(content)
    message = _phrase(msgs, sender, unread)
    ctor = Outcome.ok if msgs else Outcome.empty
    return ctor(message, capability=NAME, provider=provider, count=len(msgs), messages=msgs)
