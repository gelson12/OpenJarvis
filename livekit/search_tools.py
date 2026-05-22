"""Tiered web / YouTube / news search for the JARVIS screen widgets.

Design (per user spec): the fast, light HTTP APIs are the PRIMARY path
— Brave Search for the web, the YouTube Data API for videos. When a
key is missing, or its free tier is exhausted (HTTP 429 / 403 quota),
we transparently FALL BACK to Playwright extraction so search keeps
working. News uses the keyless Google News RSS feed.

Every function returns plain dicts ready to publish on the `jarvis-ui`
data topic — the frontend widgets read these shapes directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

_BRAVE_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
_YT_KEY = os.environ.get("YOUTUBE_API_KEY", "")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class _QuotaError(Exception):
    """Raised when an API key is rate-limited or out of quota."""


class _ApiGate:
    """Tracks whether an API should be used or skipped.

    Skipped when no key is configured, or when a recent call tripped a
    rate-limit / quota error (then it stays disabled for a cooldown so
    we don't hammer an exhausted key — we use Playwright meanwhile).
    """

    def __init__(self, name: str, has_key: bool) -> None:
        self.name = name
        self._has_key = has_key
        self._disabled_until = 0.0

    def usable(self) -> bool:
        return self._has_key and time.time() >= self._disabled_until

    def trip(self, seconds: float = 1800.0) -> None:
        self._disabled_until = time.time() + seconds
        logger.warning(
            "%s API unavailable — using Playwright fallback for %.0f min",
            self.name,
            seconds / 60.0,
        )


_brave_gate = _ApiGate("Brave Search", bool(_BRAVE_KEY))
_yt_gate = _ApiGate("YouTube Data", bool(_YT_KEY))


# ── Shared Playwright browser (lazy singleton) ───────────────────────
_pw = None
_browser = None
_browser_lock = asyncio.Lock()


async def _get_browser():
    """Return a live headless Chromium, launching it on first use."""
    global _pw, _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        logger.info("search: launched headless Chromium for fallback")
        return _browser


async def get_browser():
    """Public accessor for the shared headless Chromium instance."""
    return await _get_browser()


# ── Web search ───────────────────────────────────────────────────────
async def _brave_web(query: str, limit: int) -> list[dict]:
    headers = {"X-Subscription-Token": _BRAVE_KEY, "Accept": "application/json"}
    params = {"q": query, "count": min(limit, 20)}
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params,
        )
    if resp.status_code in (429, 403):
        _brave_gate.trip()
        raise _QuotaError(f"Brave HTTP {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    out: list[dict] = []
    for item in (data.get("web", {}).get("results") or [])[:limit]:
        out.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": (item.get("description", "") or "")[:300],
            }
        )
    return out


async def _playwright_web(query: str, limit: int) -> list[dict]:
    """Scrape Bing — more tolerant of datacentre IPs than other engines."""
    browser = await _get_browser()
    ctx = await browser.new_context(user_agent=_UA, locale="en-US")
    out: list[dict] = []
    try:
        page = await ctx.new_page()
        await page.goto(
            "https://www.bing.com/search?q=" + urllib.parse.quote(query),
            wait_until="domcontentloaded",
            timeout=20000,
        )
        for block in await page.query_selector_all("li.b_algo"):
            if len(out) >= limit:
                break
            link = await block.query_selector("h2 a")
            if link is None:
                continue
            href = await link.get_attribute("href") or ""
            title = (await link.inner_text()).strip()
            if not href or not title:
                continue
            snippet = ""
            cap = await block.query_selector(".b_caption p, .b_algoSlug, p")
            if cap is not None:
                snippet = (await cap.inner_text()).strip()
            out.append({"title": title, "url": href, "snippet": snippet[:300]})
    finally:
        await ctx.close()
    return out


async def web_search(query: str, limit: int = 6) -> list[dict]:
    """Web search — Brave API primary, Playwright/Bing fallback."""
    query = (query or "").strip()
    if not query:
        return []
    if _brave_gate.usable():
        try:
            return await _brave_web(query, limit)
        except _QuotaError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Brave web search failed: %s", exc)
    try:
        return await _playwright_web(query, limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("Playwright web search failed: %s", exc)
        return []


# ── YouTube search ───────────────────────────────────────────────────
async def _yt_api(query: str, limit: int) -> list[dict]:
    params = {
        "key": _YT_KEY,
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": min(limit, 25),
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/search", params=params
        )
    if resp.status_code in (429, 403):
        _yt_gate.trip()
        raise _QuotaError(f"YouTube HTTP {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    out: list[dict] = []
    for item in data.get("items", [])[:limit]:
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            continue
        sn = item.get("snippet") or {}
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        out.append(
            {
                "videoId": vid,
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "thumbnail": thumb or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            }
        )
    return out


async def _playwright_yt(query: str, limit: int) -> list[dict]:
    browser = await _get_browser()
    ctx = await browser.new_context(
        user_agent=_UA,
        locale="en-US",
        viewport={"width": 1280, "height": 900},
    )
    # Skip the EU cookie-consent interstitial.
    await ctx.add_cookies(
        [
            {"name": "CONSENT", "value": "YES+1", "domain": ".youtube.com", "path": "/"},
            {"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"},
        ]
    )
    out: list[dict] = []
    try:
        page = await ctx.new_page()
        await page.goto(
            "https://www.youtube.com/results?search_query="
            + urllib.parse.quote(query),
            wait_until="domcontentloaded",
            timeout=25000,
        )
        try:
            await page.wait_for_selector("ytd-video-renderer", timeout=12000)
        except Exception:  # noqa: BLE001
            return out
        for r in await page.query_selector_all("ytd-video-renderer"):
            if len(out) >= limit:
                break
            link = await r.query_selector("a#video-title")
            if link is None:
                continue
            href = await link.get_attribute("href") or ""
            if "watch?v=" not in href:
                continue
            vid = href.split("watch?v=", 1)[1].split("&", 1)[0]
            title = (await link.get_attribute("title")) or (await link.inner_text())
            channel = ""
            ch = await r.query_selector("ytd-channel-name #text, ytd-channel-name a")
            if ch is not None:
                channel = (await ch.inner_text()).strip()
            out.append(
                {
                    "videoId": vid,
                    "title": (title or "").strip(),
                    "channel": channel,
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                }
            )
    finally:
        await ctx.close()
    return out


async def youtube_search(query: str, limit: int = 8) -> list[dict]:
    """YouTube search — Data API primary, Playwright fallback."""
    query = (query or "").strip()
    if not query:
        return []
    if _yt_gate.usable():
        try:
            return await _yt_api(query, limit)
        except _QuotaError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube API search failed: %s", exc)
    try:
        return await _playwright_yt(query, limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("Playwright YouTube search failed: %s", exc)
        return []


# ── News (keyless — Google News RSS) ─────────────────────────────────
async def news_search(query: str = "", limit: int = 8) -> list[dict]:
    """Top headlines (or a topic) from the keyless Google News RSS feed."""
    query = (query or "").strip()
    if query:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
    else:
        url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    try:
        async with httpx.AsyncClient(
            timeout=12.0, headers={"User-Agent": _UA}, follow_redirects=True
        ) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.error("news fetch failed: %s", exc)
        return []
    out: list[dict] = []
    for item in root.findall(".//item")[:limit]:
        source_el = item.find("source")
        out.append(
            {
                "title": item.findtext("title", default="") or "",
                "url": item.findtext("link", default="") or "",
                "source": (source_el.text if source_el is not None else "") or "",
                "published": item.findtext("pubDate", default="") or "",
            }
        )
    return out
