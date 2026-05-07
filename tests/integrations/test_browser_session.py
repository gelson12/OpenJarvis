"""Tests for the browser session manager + tool surface.

Playwright is mocked end-to-end so tests run anywhere (no Chromium
binary needed). The actual integration test is "deploy and try it" —
the unit tests just verify our session lifecycle, error paths, and
the tool wrappers wire arguments correctly.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.integrations import browser_session
from openjarvis.integrations.browser_session import (
    BrowserSession,
    BrowserUnavailableError,
    _SESSIONS,
    _reap_stale,
    close_session,
    get_session,
    open_session,
)
from openjarvis.tools.browser_tools import (
    BrowserClickTool,
    BrowserCloseTool,
    BrowserFillTool,
    BrowserGetTextTool,
    BrowserListSessionsTool,
    BrowserNavigateTool,
    BrowserOpenTool,
)


@pytest.fixture(autouse=True)
def _clear_sessions():
    """Each test starts with a clean session map and clean env."""
    _SESSIONS.clear()
    yield
    # Tear down anything tests may have stuck in there.
    for sid in list(_SESSIONS.keys()):
        try:
            _SESSIONS[sid].close()
        except Exception:
            pass
        _SESSIONS.pop(sid, None)


def _fake_playwright():
    """Build a minimal Playwright sync_api fake. Returns the fake
    sync_playwright callable — patch into the module under test."""
    fake_page = MagicMock()
    fake_page.url = "https://example.com/"
    fake_page.title.return_value = "Example"
    fake_page.inner_text.return_value = "Hello world body text"
    fake_page.content.return_value = "<html><body>Hello</body></html>"
    fake_page.screenshot.return_value = b"PNGFAKE"
    fake_locator = MagicMock()
    fake_locator.first = fake_locator
    fake_page.locator.return_value = fake_locator

    fake_context = MagicMock()
    fake_context.new_page.return_value = fake_page

    fake_browser = MagicMock()
    fake_browser.new_context.return_value = fake_context

    fake_chromium = MagicMock()
    fake_chromium.launch.return_value = fake_browser

    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium

    fake_cm = MagicMock()
    fake_cm.__enter__ = MagicMock(return_value=fake_pw)
    fake_cm.__exit__ = MagicMock(return_value=False)

    return MagicMock(return_value=fake_cm), fake_page, fake_locator


# ---------------------------------------------------------------------------
# open_session / get_session / close_session lifecycle
# ---------------------------------------------------------------------------


def test_open_session_returns_session_with_id():
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    assert isinstance(s, BrowserSession)
    assert s.id and len(s.id) >= 8
    assert s.id in _SESSIONS


def test_open_session_raises_when_playwright_missing():
    """If `playwright` isn't installed, raise BrowserUnavailableError
    with an actionable hint."""
    import sys

    # Pretend playwright isn't importable.
    with patch.dict(sys.modules, {"playwright.sync_api": None}):
        # Force ImportError on the import inside open_session
        original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with pytest.raises(BrowserUnavailableError) as ei:
                open_session()
        assert "Playwright not installed" in str(ei.value)


def test_get_session_raises_for_unknown_id():
    with pytest.raises(BrowserUnavailableError):
        get_session("nonexistent-id")


def test_close_session_returns_true_when_existed():
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    assert close_session(s.id) is True
    assert s.id not in _SESSIONS


def test_close_session_returns_false_for_unknown():
    assert close_session("nope") is False


def test_get_session_updates_last_used():
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    s.last_used = time.time() - 100  # rewind
    fetched = get_session(s.id)
    assert fetched.last_used > time.time() - 1  # touched


def test_reap_stale_closes_idle_sessions(monkeypatch):
    monkeypatch.setenv("BROWSER_SESSION_TTL_S", "1")
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    s.last_used = time.time() - 10  # 10s ago, TTL=1s -> stale
    reaped = _reap_stale()
    assert reaped >= 1
    assert s.id not in _SESSIONS


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def test_browser_open_tool_returns_session_id():
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        out = BrowserOpenTool().execute(headless=True)
    assert out.success is True
    assert "session_id" in out.content


def test_browser_open_tool_surfaces_unavailable():
    """When Playwright is missing, the tool returns success=False.

    Patch the symbol the tool actually uses (imported at module load
    time into openjarvis.tools.browser_tools), not the source module.
    """
    with patch(
        "openjarvis.tools.browser_tools.open_session",
        side_effect=BrowserUnavailableError("Playwright not installed"),
    ):
        out = BrowserOpenTool().execute()
    assert out.success is False
    assert "browser error" in out.content


def test_browser_navigate_tool_uses_session_page():
    fake_factory, fake_page, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserNavigateTool().execute(
        session_id=s.id, url="https://example.com",
    )
    assert out.success is True
    fake_page.goto.assert_called_once()
    assert "example.com" in out.content


def test_browser_get_text_returns_body_when_no_selector():
    fake_factory, fake_page, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserGetTextTool().execute(session_id=s.id)
    assert out.success is True
    assert "Hello world body text" in out.content
    fake_page.inner_text.assert_called_with("body")


def test_browser_get_text_truncates_when_exceeds_max_chars():
    fake_factory, fake_page, _ = _fake_playwright()
    fake_page.inner_text.return_value = "x" * 50000
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserGetTextTool().execute(session_id=s.id, max_chars=100)
    assert "[truncated; original was 50000 chars]" in out.content


def test_browser_click_calls_locator():
    fake_factory, fake_page, fake_locator = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserClickTool().execute(session_id=s.id, selector="button.submit")
    assert out.success is True
    fake_locator.click.assert_called_once_with(force=False)


def test_browser_fill_passes_value():
    fake_factory, fake_page, fake_locator = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserFillTool().execute(
        session_id=s.id, selector="input[name=q]", value="search query",
    )
    assert out.success is True
    fake_locator.fill.assert_called_once_with("search query")


def test_browser_close_tool_returns_closed_true():
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserCloseTool().execute(session_id=s.id)
    assert out.success is True
    assert "closed" in out.content


def test_browser_list_sessions_includes_open_one():
    fake_factory, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", fake_factory, create=True):
        s = open_session()
    out = BrowserListSessionsTool().execute()
    assert s.id in out.content
