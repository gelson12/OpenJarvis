"""Security middleware -- HTTP security headers and request guards."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

__all__ = ["SECURITY_HEADERS", "create_security_middleware"]


def _livekit_host_from_env() -> str:
    """Return just the host portion of $LIVEKIT_URL, or '' if unset/invalid.

    Used by the CSP `connect-src` directive so the browser is allowed to
    open a WebSocket to whatever LiveKit instance we're actually pointing
    at — including self-hosted Railway / fly.io / on-prem deployments
    that aren't on the public ``*.livekit.cloud`` domain. Previously the
    CSP only whitelisted ``*.livekit.cloud`` and any self-hosted URL was
    blocked with the cryptic ``Failed to fetch``.
    """
    url = os.environ.get("LIVEKIT_URL", "").strip()
    if not url:
        return ""
    # urlparse needs a scheme; tolerate values like ``host:port`` too.
    if "://" not in url:
        url = "//" + url
    try:
        host = urlparse(url).netloc
    except Exception:  # noqa: BLE001
        return ""
    return host or ""


def _build_csp() -> str:
    """Build the Content-Security-Policy header value.

    `connect-src` is the only directive that needs to know about the
    live LiveKit instance — everything else is static. We always allow
    the public cloud hosts so a Cloud deployment keeps working, and we
    additionally allow the configured self-hosted host (if any).
    """
    extra = ""
    host = _livekit_host_from_env()
    if host:
        extra = f" https://{host} wss://{host}"

    return (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        # connect-src: LiveKit signalling + Nominatim (OSM geocoder used
        # by the maps widget to resolve a spoken place name to a bbox).
        # Without nominatim.openstreetmap.org listed here, the maps
        # widget's geocode fetch is blocked by CSP and falls through to
        # the "no map results" state.
        f"connect-src 'self' https://*.livekit.cloud wss://*.livekit.cloud "
        f"https://nominatim.openstreetmap.org{extra}; "
        "media-src 'self' blob: mediastream:; "
        "worker-src 'self' blob:; "
        # frame-src: voice widgets embed YouTube (nocookie) + vanilla
        # youtube.com, plus v0.dev / Vercel-hosted previews for the
        # voice-driven website generator (see livekit/worker.py
        # build_website), plus openstreetmap.org for the maps widget
        # iframe (Google's keyless embed is blocked at the source as of
        # 2024, so we use OSM/Nominatim instead — see maps-widget.tsx).
        # Without this the iframe falls back to default-src 'self' and
        # renders the browser's "This content is blocked" page.
        "frame-src 'self' https://www.youtube-nocookie.com "
        "https://www.youtube.com https://youtube.com "
        "https://*.vusercontent.net https://v0.dev "
        "https://*.vercel.app "
        "https://www.openstreetmap.org; "
        # img-src: YouTube thumbnails come from i.ytimg.com / i9.ytimg.com;
        # result-card thumbnails from arbitrary https hosts. data: keeps
        # inline placeholders working.
        "img-src 'self' data: blob: https:"
    )


def create_security_middleware() -> Any:
    """Create a FastAPI middleware that adds security headers.

    Returns a middleware class/callable, or None if FastAPI is not available.

    Headers added:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - X-XSS-Protection: 1; mode=block
    - Strict-Transport-Security: max-age=31536000; includeSubDomains
    - Referrer-Policy: strict-origin-when-cross-origin
    - Permissions-Policy: camera=(self), microphone=(self), geolocation=()
    - Content-Security-Policy: dynamic, see `_build_csp`

    OPTIONS requests are passed through without headers so that
    CORS preflight is not blocked.
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import Response
    except ImportError:
        return None

    # Snapshot the CSP once at middleware creation. LIVEKIT_URL is set by
    # the deploy environment and doesn't change mid-process; rebuilding
    # the string per request would just burn CPU.
    csp = _build_csp()

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            # Let CORS preflight requests pass through without
            # security headers that would conflict with CORS.
            if request.method == "OPTIONS":
                return await call_next(request)

            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            # microphone=(self) so the same-origin LiveKit voice UI can
            # call getUserMedia. camera/geolocation stay disabled.
            response.headers["Permissions-Policy"] = (
                "camera=(self), microphone=(self), geolocation=()"
            )
            response.headers["Content-Security-Policy"] = csp
            return response

    return SecurityHeadersMiddleware


# Also export the header values as constants for testing. The CSP is
# resolved at import time using the current LIVEKIT_URL env value, so
# tests must set LIVEKIT_URL before importing this module if they want
# a specific host in the CSP — otherwise the cloud-only baseline applies.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
    "Content-Security-Policy": _build_csp(),
}
