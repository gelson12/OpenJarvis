"""
PCI-safe payment handoff: send the user a checkout URL on their phone via
Telegram so card data never enters the voice stack.

See `brain/Accommodation Booking — PCI & Payment Handoff` for the design.

Phase 1 sends the raw provider checkout URL (LiteAPI's URLs are HTTPS +
tokenised). Phase 2 wraps it in a signed redirect with a 15-min TTL via
`MagicLinkBuilder` — kept here for forward compatibility.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

import httpx


@dataclass
class MagicLinkBuilder:
    """Sign an HMAC-protected redirect URL with a short TTL."""

    secret: str
    base_url: str  # e.g. "https://obsidian-mind-production-b3f9.up.railway.app"
    ttl_seconds: int = 900

    def build(self, checkout_url: str) -> str:
        payload = {"u": checkout_url, "exp": int(time.time()) + self.ttl_seconds}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        sig = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{self.base_url.rstrip('/')}/r/{body}.{sig}"

    def verify(self, token: str) -> str:
        """Reverse of build — returns the original checkout URL or raises."""
        body, _, sig = token.partition(".")
        if not sig:
            raise ValueError("malformed token")
        expected = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if int(payload["exp"]) < time.time():
            raise ValueError("expired")
        return payload["u"]


class TelegramSender:
    """Sends checkout URLs to the user's pre-registered Telegram chat.

    Reuses the existing OpenJarvis / fj2 `TELEGRAM_BOT_TOKEN` env var and the
    same contacts JSON map. Falls back to returning False (caller publishes
    to the HUD instead) on any error.
    """

    def __init__(
        self,
        bot_token: str | None = None,
        default_chat_id: str | None = None,
        timeout: float = 10.0,
    ):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.default_chat_id = default_chat_id or os.environ.get(
            "TELEGRAM_DEFAULT_CHAT_ID", ""
        ).strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.default_chat_id)

    async def send_checkout_link(
        self,
        url: str,
        property_name: str,
        price_total: float,
        currency: str,
        chat_id: str | None = None,
    ) -> bool:
        if not self.configured:
            return False
        target = chat_id or self.default_chat_id
        text = (
            f"🏨 Booking ready: *{property_name}*\n"
            f"Total: *{price_total:.2f} {currency}*\n\n"
            f"Tap to complete payment securely (link expires in 15 min):\n{url}"
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": target,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": False,
                    },
                )
                resp.raise_for_status()
                return True
            except httpx.HTTPError:
                return False
