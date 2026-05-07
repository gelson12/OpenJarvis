"""Model-callable Playwright browser tools.

The agent drives a real Chromium instance through these — open a
session, navigate, click, fill, extract, screenshot, close. Sessions
persist in process memory across tool calls within a single agent run.

Confirmation gates: most mutating browser actions (click, fill,
press_key) carry ``requires_confirmation=False`` because they're
contained inside a sandboxed browser session that doesn't touch
external systems beyond the page being navigated. Truly dangerous
ops (downloading + executing files, etc.) aren't exposed.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.browser_session import (
    BrowserUnavailableError,
    close_session,
    get_session,
    list_sessions,
    open_session,
)
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _ok(name: str, payload: Any) -> ToolResult:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = str(payload)
    return ToolResult(tool_name=name, content=payload or "(no content)", success=True)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"browser error: {exc}", success=False)


@ToolRegistry.register("browser_open")
class BrowserOpenTool(BaseTool):
    is_local = True
    tool_id = "browser_open"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_open",
            description=(
                "Launch a headless Chromium session. Returns a "
                "session_id you must pass to every subsequent "
                "browser_* tool until you browser_close. Sessions "
                "auto-expire after 10 minutes idle."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "headless": {"type": "boolean", "default": True},
                    "viewport_width": {"type": "integer", "default": 1280},
                    "viewport_height": {"type": "integer", "default": 800},
                    "user_agent": {"type": "string"},
                },
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = open_session(
                headless=bool(params.get("headless", True)),
                viewport_width=int(params.get("viewport_width", 1280)),
                viewport_height=int(params.get("viewport_height", 800)),
                user_agent=params.get("user_agent"),
            )
            return _ok(self.spec.name, {"session_id": s.id, "status": "open"})
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_navigate")
class BrowserNavigateTool(BaseTool):
    is_local = True
    tool_id = "browser_navigate"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_navigate",
            description=(
                "Navigate the session's page to a URL. Waits for "
                "load (networkidle by default). Returns the final URL "
                "(after redirects) and page title."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "url": {"type": "string"},
                    "wait_until": {
                        "type": "string",
                        "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                        "default": "networkidle",
                    },
                },
                "required": ["session_id", "url"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            s.page.goto(
                str(params["url"]),
                wait_until=str(params.get("wait_until", "networkidle")),
            )
            return _ok(
                self.spec.name,
                {"url": s.page.url, "title": s.page.title()},
            )
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_get_text")
class BrowserGetTextTool(BaseTool):
    is_local = True
    tool_id = "browser_get_text"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_get_text",
            description=(
                "Extract text from the page. Without `selector`, "
                "returns the entire page's inner_text (cleaned). With "
                "a CSS selector, returns the matched element's text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to scope extraction.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": 10000,
                        "description": "Cap return size to avoid blowing context.",
                    },
                },
                "required": ["session_id"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            selector = params.get("selector")
            if selector:
                text = s.page.locator(selector).inner_text()
            else:
                text = s.page.inner_text("body")
            max_chars = int(params.get("max_chars", 10000))
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n[truncated; original was {len(text)} chars]"
            return _ok(self.spec.name, {"text": text, "url": s.page.url})
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_get_html")
class BrowserGetHtmlTool(BaseTool):
    is_local = True
    tool_id = "browser_get_html"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_get_html",
            description=(
                "Get the page's outer HTML. Heavier than "
                "browser_get_text — use only when you need the markup "
                "structure (links, attributes, hidden inputs)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to scope output.",
                    },
                    "max_chars": {"type": "integer", "default": 30000},
                },
                "required": ["session_id"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            selector = params.get("selector")
            if selector:
                html = s.page.locator(selector).first.inner_html()
            else:
                html = s.page.content()
            max_chars = int(params.get("max_chars", 30000))
            if len(html) > max_chars:
                html = html[:max_chars] + f"\n<!-- truncated; original was {len(html)} chars -->"
            return _ok(self.spec.name, {"html": html, "url": s.page.url})
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_click")
class BrowserClickTool(BaseTool):
    is_local = True
    tool_id = "browser_click"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_click",
            description=(
                "Click a CSS selector. Waits for navigation if the "
                "click triggers it. Use Playwright selectors: tag, "
                "[attr=value], :has-text(\"...\"), >> for chains."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "force": {"type": "boolean", "default": False},
                },
                "required": ["session_id", "selector"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            s.page.locator(str(params["selector"])).first.click(
                force=bool(params.get("force", False)),
            )
            return _ok(
                self.spec.name,
                {"clicked": params["selector"], "url": s.page.url},
            )
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_fill")
class BrowserFillTool(BaseTool):
    is_local = True
    tool_id = "browser_fill"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_fill",
            description=(
                "Fill an input / textarea / contenteditable. Replaces "
                "any existing content. For typing one keystroke at a "
                "time (e.g. trigger autocomplete), use browser_press."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["session_id", "selector", "value"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            s.page.locator(str(params["selector"])).first.fill(
                str(params["value"]),
            )
            return _ok(
                self.spec.name,
                {"filled": params["selector"], "value_len": len(params["value"])},
            )
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_press")
class BrowserPressTool(BaseTool):
    is_local = True
    tool_id = "browser_press"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_press",
            description=(
                "Press a keyboard key (or chord) on the focused or "
                "specified element. Examples: 'Enter', 'Tab', "
                "'Control+A', 'ArrowDown'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "key": {"type": "string"},
                    "selector": {
                        "type": "string",
                        "description": "Optional — focus this selector first.",
                    },
                },
                "required": ["session_id", "key"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            key = str(params["key"])
            sel = params.get("selector")
            if sel:
                s.page.locator(str(sel)).first.press(key)
            else:
                s.page.keyboard.press(key)
            return _ok(self.spec.name, {"pressed": key})
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_wait_for")
class BrowserWaitForTool(BaseTool):
    is_local = True
    tool_id = "browser_wait_for"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_wait_for",
            description=(
                "Wait for a CSS selector to appear / become visible / "
                "be hidden. Use when the page loads content "
                "asynchronously (SPAs, AJAX dashboards)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["attached", "detached", "visible", "hidden"],
                        "default": "visible",
                    },
                    "timeout_ms": {"type": "integer", "default": 15000},
                },
                "required": ["session_id", "selector"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            s = get_session(str(params["session_id"]))
            s.page.locator(str(params["selector"])).wait_for(
                state=str(params.get("state", "visible")),
                timeout=int(params.get("timeout_ms", 15000)),
            )
            return _ok(
                self.spec.name,
                {"waited_for": params["selector"], "state": params.get("state", "visible")},
            )
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_screenshot")
class BrowserScreenshotTool(BaseTool):
    is_local = True
    tool_id = "browser_screenshot"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_screenshot",
            description=(
                "Take a PNG screenshot of the page (or a selector). "
                "Returns a base64 data URL plus byte size — the agent "
                "can pass it to a vision model or save to disk."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "selector": {
                        "type": "string",
                        "description": "Optional — clip to this element.",
                    },
                    "full_page": {"type": "boolean", "default": False},
                },
                "required": ["session_id"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            import base64

            s = get_session(str(params["session_id"]))
            selector = params.get("selector")
            if selector:
                buf = s.page.locator(str(selector)).first.screenshot()
            else:
                buf = s.page.screenshot(full_page=bool(params.get("full_page", False)))
            b64 = base64.b64encode(buf).decode()
            return _ok(
                self.spec.name,
                {
                    "data_url": f"data:image/png;base64,{b64}",
                    "bytes": len(buf),
                    "url": s.page.url,
                },
            )
        except BrowserUnavailableError as exc:
            return _err(self.spec.name, exc)
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_close")
class BrowserCloseTool(BaseTool):
    is_local = True
    tool_id = "browser_close"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_close",
            description=(
                "Close the session and free Chromium memory. ALWAYS "
                "call this when you're done with a session."
            ),
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            ok = close_session(str(params["session_id"]))
            return _ok(self.spec.name, {"closed": ok, "session_id": params["session_id"]})
        except Exception as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("browser_list_sessions")
class BrowserListSessionsTool(BaseTool):
    is_local = True
    tool_id = "browser_list_sessions"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="browser_list_sessions",
            description=(
                "Diagnostic — list active browser sessions, their "
                "current URL, and idle time. Useful when an agent "
                "loses track of a session_id."
            ),
            parameters={"type": "object", "properties": {}},
            category="browser",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, {"sessions": list_sessions()})
        except Exception as exc:
            return _err(self.spec.name, exc)


__all__ = [
    "BrowserOpenTool",
    "BrowserNavigateTool",
    "BrowserGetTextTool",
    "BrowserGetHtmlTool",
    "BrowserClickTool",
    "BrowserFillTool",
    "BrowserPressTool",
    "BrowserWaitForTool",
    "BrowserScreenshotTool",
    "BrowserCloseTool",
    "BrowserListSessionsTool",
]
