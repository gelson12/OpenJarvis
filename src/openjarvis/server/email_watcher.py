"""Persistent email-watch system.

User says: "Let me know when Pedro emails me"
  → adds a watch record {sender:"Pedro", added_at:..., last_check_seen:...}

A background poller checks every 60s for new emails matching ANY active
watch. When a match is found, it:
  1. Marks the watch as "triggered" with the matched message id
  2. Publishes a notification event via the LiveKit data channel
     (topic: "jarvis-watch-alert") so the worker can speak an alert
  3. The voice worker then plays: "Sir — Pedro has sent you an email
     titled '<subject>'. Would you like me to read it or open the panel?"

Watches persist to ~/.openjarvis/email_watches.json.

Public API:
    add_watch(sender, provider="outlook") -> str (watch_id)
    list_watches(active_only=True) -> list[dict]
    remove_watch(watch_id) -> bool
    check_now() -> list[dict]  (matches found across all watches)
    start_poller(notify_callback) -> None  (called on app startup)

Env gate: OPENJARVIS_EMAIL_WATCHER_ENABLED (default true)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_POLLER_THREAD: Optional[threading.Thread] = None
_NOTIFY_CB: Optional[Callable[[Dict[str, Any]], None]] = None


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_EMAIL_WATCHER_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _poll_interval() -> int:
    try:
        return int(os.environ.get("OPENJARVIS_EMAIL_WATCHER_INTERVAL_SEC", "60"))
    except Exception:
        return 60


def _store_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "email_watches.json"


def _load() -> Dict[str, Any]:
    p = _store_path()
    if not p.exists():
        return {"watches": []}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {"watches": []}
    except Exception as exc:
        logger.warning("email_watcher: load failed (%s) — starting fresh", exc)
        return {"watches": []}


def _save(data: Dict[str, Any]) -> None:
    try:
        _store_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("email_watcher: save failed: %s", exc)


def _make_id(sender: str, ts: float) -> str:
    raw = f"{sender.lower().strip()}::{int(ts)}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:10]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_watch(sender: str = "", *, provider: str = "outlook",
              subject_contains: str = "",
              folders: Optional[List[str]] = None) -> Optional[str]:
    """Add a watch. Returns the watch_id or None if invalid input.

    A watch must specify at least one of `sender` or `subject_contains`.
    `folders` is a list of Outlook well-known folder names to scan; defaults
    to ['inbox']. Use ['inbox', 'junkemail'] to also check junk mail.
    """
    sender = (sender or "").strip()
    subject_contains = (subject_contains or "").strip()
    # Round 9-followup: support topic-only watches (no sender required).
    if not sender and not subject_contains:
        return None
    if provider not in ("outlook", "gmail"):
        provider = "outlook"
    # Default + sanitize folder list
    if not folders:
        folders = ["inbox"]
    valid_outlook = {"inbox", "junkemail", "drafts", "sentitems",
                     "deleteditems", "archive"}
    folders = [f.lower() for f in folders if isinstance(f, str)]
    if provider == "outlook":
        folders = [f for f in folders if f in valid_outlook] or ["inbox"]
    # Deduplicate, preserve order
    seen: set = set()
    folders = [f for f in folders if not (f in seen or seen.add(f))]
    now = time.time()
    # Include subject + folders in the id seed so distinct watches with the
    # same sender don't collide.
    wid = _make_id(f"{sender}|{subject_contains}|{','.join(folders)}", now)
    watch = {
        "id": wid,
        "sender": sender,
        "subject_contains": subject_contains,
        "folders": folders,
        "provider": provider,
        "active": True,
        "triggered": False,
        "added_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "added_ts": now,
        "last_check_ts": 0.0,
        "matched_message_id": None,
        "matched_subject": None,
        "matched_at": None,
    }
    with _LOCK:
        data = _load()
        data["watches"].append(watch)
        _save(data)
    logger.info(
        "openjarvis.email_watcher.add id=%s sender=%r subject=%r "
        "folders=%s provider=%s",
        wid, sender[:60], subject_contains[:60], folders, provider,
    )
    return wid


def list_watches(active_only: bool = True) -> List[Dict[str, Any]]:
    data = _load()
    items = data.get("watches", [])
    if active_only:
        items = [w for w in items if w.get("active")]
    return items


def remove_watch(watch_id: str) -> bool:
    if not watch_id:
        return False
    with _LOCK:
        data = _load()
        before = len(data.get("watches", []))
        data["watches"] = [w for w in data.get("watches", []) if w.get("id") != watch_id]
        after = len(data["watches"])
        if after != before:
            _save(data)
            return True
    return False


def deactivate_watch(watch_id: str) -> bool:
    """Soft-remove — keep the record but stop polling. Used after a notification
    fires so we don't keep alerting on the same email."""
    if not watch_id:
        return False
    with _LOCK:
        data = _load()
        changed = False
        for w in data.get("watches", []):
            if w.get("id") == watch_id and w.get("active"):
                w["active"] = False
                changed = True
                break
        if changed:
            _save(data)
        return changed


def mark_notified(watch_id: str) -> bool:
    """Flag that the user has been told about this watch's trigger so we
    don't repeat the alert on every subsequent turn."""
    if not watch_id:
        return False
    with _LOCK:
        data = _load()
        changed = False
        for w in data.get("watches", []):
            if w.get("id") == watch_id and not w.get("notified"):
                w["notified"] = True
                w["notified_at"] = datetime.now(timezone.utc).isoformat()
                w["active"] = False  # stop polling for this one
                changed = True
                break
        if changed:
            _save(data)
        return changed


# ---------------------------------------------------------------------------
# Provider-side check
# ---------------------------------------------------------------------------

def _check_outlook(
    sender: str = "",
    subject_contains: str = "",
    folders: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Returns list of matching messages [{id, subject, sender, received_at,
    folder}] scanned across the requested folders (defaults to ['inbox'])."""
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("outlook_list_messages")
        if cls is None:
            return []
        filters: List[str] = []
        if sender:
            if "@" in sender:
                filters.append(f"from/emailAddress/address eq '{sender}'")
            else:
                # Escape single quotes by doubling per OData syntax
                safe = sender.replace("'", "''")
                filters.append(f"contains(from/emailAddress/name,'{safe}')")
        if subject_contains:
            safe = subject_contains.replace("'", "''")
            filters.append(f"contains(subject,'{safe}')")
        # Always restrict to "recent" so we don't keep matching the same
        # stale message forever (a 24h window covers all polling cadences).
        from datetime import timedelta
        recent_iso = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        filters.append(f"receivedDateTime ge {recent_iso}")
        folders = folders or ["inbox"]
        out: List[Dict[str, Any]] = []
        for folder in folders:
            kwargs: Dict[str, Any] = {"top": 5}
            if folder and folder != "inbox":
                kwargs["folder_id"] = folder
            elif folder == "inbox":
                # Explicit inbox is OK; tool defaults there if omitted, but
                # being explicit makes the request self-documenting.
                kwargs["folder_id"] = "inbox"
            if filters:
                kwargs["filter"] = " and ".join(filters)
            try:
                result = cls().execute(**kwargs)
            except Exception as exc:
                logger.warning(
                    "email_watcher: outlook check failed folder=%s: %s",
                    folder, exc,
                )
                continue
            content = getattr(result, "content", "") or ""
            try:
                parsed = json.loads(content)
            except Exception:
                continue
            msgs = parsed.get("value") if isinstance(parsed, dict) else parsed
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                out.append({
                    "id": m.get("id"),
                    "subject": (m.get("subject") or "").strip(),
                    "sender": (m.get("from", {}).get("emailAddress", {}).get("name")
                               or m.get("from", {}).get("emailAddress", {}).get("address")
                               or sender or "(unknown)"),
                    "received_at": m.get("receivedDateTime"),
                    "folder": folder,
                })
        return out
    except Exception as exc:
        logger.warning("email_watcher: outlook check failed: %s", exc)
        return []


def _check_gmail(sender: str, subject_contains: str = "") -> List[Dict[str, Any]]:
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get("gmail_list_messages")
        if cls is None:
            return []
        parts = [f"from:{sender}"]
        if subject_contains:
            parts.append(f"subject:({subject_contains})")
        parts.append("newer_than:1d")
        q = " ".join(parts)
        result = cls().execute(q=q)
        content = getattr(result, "content", "") or ""
        try:
            parsed = json.loads(content)
        except Exception:
            return []
        msgs = parsed.get("messages") if isinstance(parsed, dict) else parsed
        if not isinstance(msgs, list):
            return []
        out = []
        for m in msgs[:5]:
            out.append({
                "id": m.get("id"),
                "subject": "",  # would need gmail_get_message to enrich
                "sender": sender,
                "received_at": None,
            })
        return out
    except Exception as exc:
        logger.warning("email_watcher: gmail check failed: %s", exc)
        return []


def check_now() -> List[Dict[str, Any]]:
    """Run one check across all active watches. Returns list of triggered
    matches (each with watch + first message)."""
    matches: List[Dict[str, Any]] = []
    with _LOCK:
        data = _load()
        for w in data.get("watches", []):
            if not w.get("active") or w.get("triggered"):
                continue
            provider = w.get("provider", "outlook")
            sender = w.get("sender", "") or ""
            subj = w.get("subject_contains", "") or ""
            folders = w.get("folders") or ["inbox"]
            if not sender and not subj:
                # Invalid watch (legacy or corrupted) — skip silently
                continue
            if provider == "gmail":
                hits = _check_gmail(sender, subj)
            else:
                hits = _check_outlook(sender, subj, folders=folders)
            w["last_check_ts"] = time.time()
            if hits:
                first = hits[0]
                w["triggered"] = True
                w["matched_message_id"] = first.get("id")
                w["matched_subject"] = first.get("subject")
                w["matched_folder"] = first.get("folder")
                w["matched_at"] = datetime.now(timezone.utc).isoformat()
                matches.append({"watch": dict(w), "message": first})
        if matches:
            _save(data)
    return matches


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

def _poller_loop() -> None:
    interval = _poll_interval()
    logger.info("openjarvis.email_watcher.poller started interval=%ds", interval)
    while True:
        try:
            time.sleep(interval)
        except Exception:
            time.sleep(60)
        if not _enabled():
            continue
        try:
            matches = check_now()
        except Exception as exc:
            logger.warning("email_watcher: check_now failed: %s", exc)
            continue
        if not matches:
            continue
        cb = _NOTIFY_CB
        if cb is None:
            logger.info(
                "openjarvis.email_watcher.matches_pending count=%d "
                "(no notify callback registered)", len(matches),
            )
            continue
        for match in matches:
            try:
                cb(match)
            except Exception as exc:
                logger.warning("email_watcher: notify cb failed: %s", exc)


def start_poller(notify_callback: Callable[[Dict[str, Any]], None]) -> None:
    """Idempotent — starts the daemon thread once. The callback receives
    each match dict {watch, message} and is responsible for delivery
    (e.g. publishing to a LiveKit data channel)."""
    global _POLLER_THREAD, _NOTIFY_CB
    _NOTIFY_CB = notify_callback
    if _POLLER_THREAD is not None and _POLLER_THREAD.is_alive():
        return
    if not _enabled():
        logger.info("openjarvis.email_watcher.poller disabled by env")
        return
    t = threading.Thread(target=_poller_loop,
                         name="openjarvis-email-watcher",
                         daemon=True)
    t.start()
    _POLLER_THREAD = t


# ---------------------------------------------------------------------------
# Intent detection — "let me know when X emails me"
# ---------------------------------------------------------------------------

_WATCH_INTENT_RE = re.compile(
    r"\b("
    # "let me know / notify / tell / alert / monitor / be on the lookout
    #  / keep an eye / look out for" + when/if + email reference
    r"(?:let\s+me\s+know|notify\s+me|tell\s+me|alert\s+me|warn\s+me|"
    r"watch\s+(?:for|out\s+for)?|keep\s+an?\s+eye(?:\s+out)?|"
    r"monitor\s+for|"
    r"be\s+on\s+(?:the\s+)?lookout|on\s+the\s+lookout|"
    r"trigger\s+(?:on|when)|"
    r"set\s+up\s+(?:an?\s+)?(?:alert|reminder|watch|monitor|trigger))\s+"
    r"(?:.{0,80})?(?:when|if|for)\s+(?:.{0,120})?\b(?:emails?|messages?|mail|inbox|"
    r"junk(?:mail)?|spam|anything|something)"
    r"|"
    # "wait for an email about X" / "waiting for VIQU to email"
    # The verb consumes the connector 'for' so we can't require a second
    # when/if/for. Match the verb directly followed by email-domain noun.
    r"(?:wait(?:ing)?\s+for|expecting|anticipating)\s+(?:an?\s+|the\s+)?"
    r"(?:emails?|messages?|mail|reply|response)"
    r"|"
    # "watch my inbox for", "set up an alert for emails from X"
    r"(?:watch|monitor|notify\s+me\s+about)\s+(?:my\s+)?"
    r"(?:inbox|email|emails?|junk(?:mail)?|spam)\s+for"
    r"|"
    # Direct trigger: "if X emails me let me know"
    r"if\s+\S+\s+emails?\s+me"
    r")\b",
    re.IGNORECASE,
)

_WATCH_FROM_RE = re.compile(
    # Order matters: full email address first (otherwise the proper-name
    # alternative grabs just the local part under re.IGNORECASE). Then
    # all-caps acronyms (e.g. "VIQU"), then proper-case names.
    r"\bfrom\s+(\S+@\S+\.\S+|[A-Z]{2,}|[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
    re.IGNORECASE,
)
# Catch "when Pedro emails me" / "Pedro sends me" / "Pedro's email" — i.e.
# a capitalised proper name immediately followed by an email-action verb.
_WATCH_NAME_EMAILS_RE = re.compile(
    r"\b(?:when\s+|if\s+)?"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|[A-Z]{2,})"
    r"(?:'s\s+|\s+)"
    r"(?:emails?(?:\s+me)?|sends?(?:\s+me)?(?:\s+(?:an?\s+)?(?:email|message))?|"
    r"messages?(?:\s+me)?|writes?(?:\s+me)?|gets?\s+back\s+to\s+me)",
)
# Plain email address anywhere in the text.
_WATCH_EMAIL_ADDR_RE = re.compile(r"\b([\w.+-]+@[\w-]+\.[\w.-]+)\b", re.IGNORECASE)

# Topic/subject extraction. The phrasings span many forms — we keep this
# focused on patterns that clearly delimit a topic phrase.
_WATCH_TOPIC_RE = re.compile(
    r"\b(?:"
    r"about|regarding|concerning|on\s+the\s+(?:topic|subject)\s+of|"
    r"with\s+(?:the\s+)?(?:subject|topic|title)|"
    # Mention verbs in any form: mention/mentions/mentioning/mentioned/that-mentions
    r"mentioning|(?:that\s+)?mention(?:s|ed)?|"
    r"containing|with\s+the\s+(?:word|phrase|term)|"
    r"to\s+do\s+with"
    r")\s+"
    r"(?:['\"]([^'\"\n]{2,80})['\"]"            # quoted phrase wins
    r"|([A-Za-z][\w\s.&'-]{1,60}?)"             # else greedy stop on punctuation
    r")(?=$|[.,!?;:]|\s+(?:lands?|arrives?|comes?|shows?|appears?|in\s+(?:my|the)|"
    r"and|or|but)\b)",
    re.IGNORECASE,
)

# Folder hint extraction. "junkmail", "junk mail", "spam", "spam folder",
# "junk folder" → tell the watcher to include the JunkEmail folder.
_WATCH_JUNK_RE = re.compile(
    r"\b(junk\s*mail|junk\s+folder|junk|spam(?:\s+folder)?)\b",
    re.IGNORECASE,
)
# Explicit inbox mention (rare; mostly we default).
_WATCH_INBOX_RE = re.compile(r"\b(inbox|mailbox)\b", re.IGNORECASE)


def _extract_folders(text: str) -> List[str]:
    """Return the list of Outlook folders to scan based on the user's
    phrasing. Defaults to ['inbox']. 'junkmail' / 'spam' adds 'junkemail'.
    If BOTH are mentioned, both are scanned. The order preserved is
    inbox-first so most relevant matches surface first."""
    has_junk = bool(_WATCH_JUNK_RE.search(text or ""))
    has_inbox = bool(_WATCH_INBOX_RE.search(text or ""))
    if has_junk and has_inbox:
        return ["inbox", "junkemail"]
    if has_junk:
        return ["junkemail"]
    return ["inbox"]


def detect_watch_intent(text: str) -> Optional[Dict[str, Any]]:
    """Returns {sender, provider, subject_contains, folders} if the user
    wants to set up an email watch. Either `sender` or `subject_contains`
    must be present — topic-only watches are now supported."""
    if not text or not _WATCH_INTENT_RE.search(text):
        return None
    # Sender extraction — try in priority order: "from <Name>" → name-emails-me
    # → bare email address.
    sender = ""
    m = _WATCH_FROM_RE.search(text)
    if m:
        sender = m.group(1).strip()
    if not sender:
        m2 = _WATCH_NAME_EMAILS_RE.search(text)
        if m2:
            sender = m2.group(1).strip()
    if not sender:
        m3 = _WATCH_EMAIL_ADDR_RE.search(text)
        if m3:
            sender = m3.group(1).strip()

    # Topic extraction — independent of sender.
    subject = ""
    mt = _WATCH_TOPIC_RE.search(text)
    if mt:
        # group 1 = quoted, group 2 = unquoted
        subject = (mt.group(1) or mt.group(2) or "").strip(" .,;:!?'\"")

    # Need at least one signal — sender OR subject.
    if not sender and not subject:
        return None

    provider = "outlook"
    if re.search(r"\bgmail\b", text, re.I):
        provider = "gmail"

    folders = _extract_folders(text)

    return {
        "sender": sender,
        "subject_contains": subject,
        "provider": provider,
        "folders": folders,
    }


# ---------------------------------------------------------------------------
# Snapshot for /v1/_debug/agentic
# ---------------------------------------------------------------------------

def snapshot() -> Dict[str, Any]:
    data = _load()
    watches = data.get("watches", [])
    return {
        "enabled": _enabled(),
        "poll_interval_sec": _poll_interval(),
        "active_watches": [w for w in watches if w.get("active")],
        "triggered_watches": [w for w in watches if w.get("triggered")],
        "total": len(watches),
        "poller_running": _POLLER_THREAD is not None and _POLLER_THREAD.is_alive(),
    }
