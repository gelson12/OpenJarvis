"""Live remote-browser session for the JARVIS `browser` widget.

A single Playwright page, streamed to the frontend as JPEG frames and
driven by relayed click / scroll / key / navigation events. Reuses the
shared headless Chromium launched by search_tools.
"""

from __future__ import annotations

import logging

import search_tools

logger = logging.getLogger(__name__)

# Streamed page viewport — the frontend sends clicks as fractions of
# this, so the exact numbers only need to be a sensible 16:10-ish shape.
VIEW_W = 1000
VIEW_H = 640


class BrowserSession:
    """One live, interactive Playwright page."""

    def __init__(self) -> None:
        self._ctx = None
        self._page = None

    @staticmethod
    def _normalize(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return "https://www.google.com"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    async def open(self, url: str) -> None:
        browser = await search_tools.get_browser()
        self._ctx = await browser.new_context(
            viewport={"width": VIEW_W, "height": VIEW_H}
        )
        self._page = await self._ctx.new_page()
        await self.navigate(url)

    async def navigate(self, url: str) -> None:
        if self._page is None:
            return
        try:
            await self._page.goto(
                self._normalize(url), wait_until="domcontentloaded", timeout=25000
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser navigate failed: %s", exc)

    async def back(self) -> None:
        if self._page is None:
            return
        try:
            await self._page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001
            pass

    async def reload(self) -> None:
        if self._page is None:
            return
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:  # noqa: BLE001
            pass

    async def click(self, x_frac: float, y_frac: float) -> None:
        if self._page is None:
            return
        try:
            await self._page.mouse.click(
                max(0.0, min(1.0, x_frac)) * VIEW_W,
                max(0.0, min(1.0, y_frac)) * VIEW_H,
            )
        except Exception:  # noqa: BLE001
            pass

    async def scroll(self, dy: float) -> None:
        if self._page is None:
            return
        try:
            await self._page.mouse.wheel(0, dy)
        except Exception:  # noqa: BLE001
            pass

    async def key(self, key: str) -> None:
        if self._page is None or not key:
            return
        try:
            if len(key) == 1:
                await self._page.keyboard.type(key)
            else:
                await self._page.keyboard.press(key)
        except Exception:  # noqa: BLE001
            pass

    async def screenshot(self) -> bytes | None:
        if self._page is None:
            return None
        try:
            return await self._page.screenshot(type="jpeg", quality=55)
        except Exception as exc:  # noqa: BLE001
            logger.debug("browser screenshot failed: %s", exc)
            return None

    @property
    def url(self) -> str:
        try:
            return self._page.url if self._page is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    async def close(self) -> None:
        ctx, self._ctx, self._page = self._ctx, None, None
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
