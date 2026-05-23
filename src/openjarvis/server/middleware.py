"""Security middleware -- HTTP security headers and request guards."""

from __future__ import annotations

from typing import Any

__all__ = ["SECURITY_HEADERS", "create_security_middleware"]


def create_security_middleware() -> Any:
    """Create a FastAPI middleware that adds security headers.

    Returns a middleware class/callable, or None if FastAPI is not available.

    Headers added:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - X-XSS-Protection: 1; mode=block
    - Strict-Transport-Security: max-age=31536000; includeSubDomains
    - Referrer-Policy: strict-origin-when-cross-origin
    - Permissions-Policy: camera=(), microphone=(), geolocation=()

    OPTIONS requests are passed through without headers so that
    CORS preflight is not blocked.
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import Response
    except ImportError:
        return None

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
            # connect-src/media-src/worker-src added so the browser can
            # reach the LiveKit signalling server (wss) + its audio
            # worklets (blob:). Without this the LiveKit client fails with
            # "could not establish signal connection: Failed to fetch".
            response.headers["Content-Security-Policy"] = (
                "default-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "connect-src 'self' https://*.livekit.cloud "
                "wss://*.livekit.cloud; "
                "media-src 'self' blob: mediastream:; "
                "worker-src 'self' blob:; "
                # frame-src: voice widgets embed YouTube (nocookie) +
                # vanilla youtube.com. Without this the iframe falls back
                # to default-src 'self' and renders the browser's
                # "content blocked" page.
                "frame-src 'self' https://www.youtube-nocookie.com "
                "https://www.youtube.com https://youtube.com; "
                # img-src: YouTube thumbnails come from i.ytimg.com /
                # i9.ytimg.com; result-card thumbnails from arbitrary
                # https hosts. data: keeps inline placeholders working.
                "img-src 'self' data: blob: https:"
            )
            return response

    return SecurityHeadersMiddleware


# Also export the header values as constants for testing
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "connect-src 'self' https://*.livekit.cloud wss://*.livekit.cloud; "
        "media-src 'self' blob: mediastream:; "
        "worker-src 'self' blob:; "
        "frame-src 'self' https://www.youtube-nocookie.com "
        "https://www.youtube.com https://youtube.com; "
        "img-src 'self' data: blob: https:"
    ),
}
