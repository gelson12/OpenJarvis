"""Web search tool — free via DuckDuckGo HTML endpoint (no API key).

Why DuckDuckGo
--------------
- Free, no API key, no signup
- Returns clean title + URL + snippet for each hit
- Less aggressive about rate-limiting than scraping Google directly
- Well-known stable HTML structure that works with simple BS4 parsing

If the user wants higher-quality results later, set `BRAVE_API_KEY` and
we can add a Brave-Search-backed tool — same interface, better quality
on long-tail queries. For now, this covers "look up X on the web" use
cases without any operator setup.

Reference: https://html.duckduckgo.com/html/?q=test
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = (
    "Mozilla/5.0 (compatible; OpenJarvis/1.0; "
    "+https://github.com/gelson12/OpenJarvis)"
)
_TIMEOUT = 10.0


def _ok(name: str, payload: Any) -> ToolResult:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = str(payload)
    return ToolResult(tool_name=name, content=payload or "(no content)", success=True)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"web_search error: {exc}", success=False)


def _unwrap_ddg_redirect(href: str) -> str:
    """DuckDuckGo wraps result links in /l/?uddg=<encoded-url>. Unwrap."""
    if not href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path == "/l/" or parsed.path.endswith("/l/"):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg", [None])[0]
        if target:
            return unquote(target)
    return href


@ToolRegistry.register("web_search")
class WebSearchTool(BaseTool):
    """Search the web. Use for current events / unfamiliar topics / fact
    checking — anything beyond the model's training cutoff."""

    tool_id = "web_search"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_search",
            description=(
                "Search the public web (DuckDuckGo). Use for current "
                "events, unfamiliar topics, fact checking, finding "
                "documentation URLs — anything beyond the model's "
                "training cutoff. Returns a list of {title, url, snippet}. "
                "Pair with browser_navigate + browser_get_text to read "
                "the full content of any result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 8, max 20).",
                    },
                },
                "required": ["query"],
            },
            category="web",
        )

    def execute(self, **params: Any) -> ToolResult:
        query = (params.get("query") or "").strip()
        if not query:
            return _err(self.spec.name, ValueError("query is required"))
        limit = max(1, min(20, int(params.get("limit", 8))))

        try:
            with httpx.Client(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            ) as c:
                resp = c.post(_DDG_HTML_URL, data={"q": query})
                resp.raise_for_status()
                html = resp.text
        except httpx.HTTPError as exc:
            return _err(self.spec.name, exc)

        # Parse with BeautifulSoup if available; else fall back to a
        # simple regex pass. BS4 is a soft dep through other tools.
        results: list[dict[str, Any]] = []
        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            for hit in soup.select("div.result")[:limit]:
                a = hit.select_one("a.result__a")
                snippet_el = hit.select_one(".result__snippet")
                if not a:
                    continue
                results.append(
                    {
                        "title": a.get_text(strip=True),
                        "url": _unwrap_ddg_redirect(a.get("href", "")),
                        "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                    }
                )
        except ImportError:
            import re

            link_re = re.compile(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            for m in link_re.finditer(html):
                if len(results) >= limit:
                    break
                href = _unwrap_ddg_redirect(m.group(1))
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                results.append({"title": title, "url": href, "snippet": ""})

        if not results:
            return _ok(
                self.spec.name,
                {"query": query, "results": [], "note": "no matches found"},
            )
        return _ok(self.spec.name, {"query": query, "results": results})


__all__ = ["WebSearchTool"]
