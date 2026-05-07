"""Headless browser sessions for the agent — backed by Playwright + Chromium.

Why a session manager
---------------------
Browsing is multi-step: open → navigate → click → fill → extract → close.
We need state to live across tool calls within a single agent run so the
agent can drive a real workflow ("log into the dashboard, click Reports,
download CSV"). Each session is identified by a short id; the agent keeps
the id in its own context and passes it back into every subsequent
``browser_*`` tool call.

Lifecycle
---------
- ``open()`` spins up a Chromium instance + a single page, returns a
  ``BrowserSession`` whose ``id`` is what tools reference.
- Sessions live in a module-level dict (``_SESSIONS``) keyed by id.
- Stale sessions (no activity in BROWSER_SESSION_TTL_S, default 600s)
  are reaped on every new ``open()`` call — no background thread needed,
  just lazy cleanup.
- ``close(session_id)`` releases Chromium memory immediately. The agent
  should call this when it's done, but if it doesn't the TTL reap handles it.
- Server restart loses every session — fine, the agent retries.

Why sync Playwright (not async)
-------------------------------
``BaseTool.execute()`` is synchronous, and the agent runs inside
``asyncio.to_thread`` already. Sync Playwright runs cleanly in that
worker thread; async Playwright would need its own event loop and
add complexity. We're not gaining anything from concurrent browsing
within a single agent run.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


class BrowserUnavailableError(RuntimeError):
    """Raised when Playwright isn't installed, or the browser launch failed."""


def _ttl_s() -> float:
    try:
        return float(os.environ.get("BROWSER_SESSION_TTL_S", "600"))
    except ValueError:
        return 600.0


def _default_timeout_ms() -> int:
    try:
        return int(os.environ.get("BROWSER_DEFAULT_TIMEOUT_MS", "15000"))
    except ValueError:
        return 15000


@dataclass
class BrowserSession:
    """A live Playwright session — owns one Chromium instance + one page.

    Held by reference in :data:`_SESSIONS`. Methods are thin wrappers
    over Playwright's sync API to keep the per-tool code uniform.
    """

    id: str
    playwright: Any  # type: PlaywrightContextManager (lazy import)
    browser: Any  # type: Browser
    context: Any  # type: BrowserContext
    page: Any  # type: Page
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()

    def close(self) -> None:
        """Release every resource. Idempotent — safe to call twice."""
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright is not None:
                self.playwright.__exit__(None, None, None)
        except Exception:
            pass
        self.context = None
        self.browser = None
        self.page = None
        self.playwright = None


_SESSIONS: dict[str, BrowserSession] = {}


def _reap_stale() -> int:
    """Close sessions idle longer than TTL. Returns count reaped."""
    now = time.time()
    ttl = _ttl_s()
    stale = [
        sid for sid, s in _SESSIONS.items() if now - s.last_used > ttl
    ]
    for sid in stale:
        try:
            _SESSIONS[sid].close()
        except Exception:
            pass
        _SESSIONS.pop(sid, None)
    if stale:
        logger.info("browser: reaped %d stale session(s)", len(stale))
    return len(stale)


def open_session(
    *,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    user_agent: Optional[str] = None,
) -> BrowserSession:
    """Launch Chromium and return a new BrowserSession."""
    _reap_stale()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailableError(
            "Playwright not installed. Add 'playwright' to the runtime "
            "image and run `playwright install chromium --with-deps`."
        ) from exc

    pw_cm = sync_playwright()
    pw = pw_cm.__enter__()
    try:
        browser = pw.chromium.launch(headless=headless)
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": viewport_width, "height": viewport_height},
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        context = browser.new_context(**context_kwargs)
        context.set_default_timeout(_default_timeout_ms())
        page = context.new_page()
    except Exception:
        # Tear down anything that did start before re-raising.
        try:
            pw_cm.__exit__(None, None, None)
        except Exception:
            pass
        raise

    session_id = uuid.uuid4().hex[:12]
    session = BrowserSession(
        id=session_id,
        playwright=pw_cm,
        browser=browser,
        context=context,
        page=page,
    )
    _SESSIONS[session_id] = session
    logger.info("browser: opened session %s (headless=%s)", session_id, headless)
    return session


def get_session(session_id: str) -> BrowserSession:
    """Look up an existing session by id; raise if not found."""
    s = _SESSIONS.get(session_id)
    if s is None:
        raise BrowserUnavailableError(
            f"browser session {session_id!r} not found — it may have "
            "expired or never been opened. Call browser_open first."
        )
    s.touch()
    return s


def close_session(session_id: str) -> bool:
    """Close and forget a session. Returns True if it existed."""
    s = _SESSIONS.pop(session_id, None)
    if s is None:
        return False
    s.close()
    logger.info("browser: closed session %s", session_id)
    return True


def list_sessions() -> list[dict[str, Any]]:
    """Diagnostic — list active sessions and their idle time."""
    now = time.time()
    return [
        {
            "id": s.id,
            "created_at": int(s.created_at),
            "idle_seconds": int(now - s.last_used),
            "url": _safe_url(s),
        }
        for s in _SESSIONS.values()
    ]


def _safe_url(session: BrowserSession) -> str:
    try:
        return session.page.url if session.page else ""
    except Exception:
        return ""


__all__ = [
    "BrowserSession",
    "BrowserUnavailableError",
    "close_session",
    "get_session",
    "list_sessions",
    "open_session",
]
