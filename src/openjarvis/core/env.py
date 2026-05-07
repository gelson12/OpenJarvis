"""Centralized env-var registry, fallback-chain reader, and alias pass.

Why this exists
---------------
1. Several Railway service variables use non-canonical casing — notably
   ``OpenAI_API`` (the OpenAI SDK reads ``OPENAI_API_KEY`` only) and
   ``Bridge_Zbigmodel_api`` (already handled ad-hoc in ``cloud.py``).
   :func:`apply_aliases` runs once at module import and copies aliased
   values into their canonical names so downstream code using raw
   ``os.environ.get`` keeps working without per-call-site changes.
2. :func:`get_env` is a shared fallback-chain reader for new call sites
   (mirrors the existing ``cloud.py:_first_env`` pattern but exposed as
   a public utility).
3. :data:`ENV_REGISTRY` is the single source of truth used by the
   ``/v1/integrations/status`` endpoint to render per-integration health
   in the frontend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class EnvSpec:
    """Metadata for a known environment variable."""

    name: str
    aliases: tuple[str, ...] = ()
    purpose: str = ""
    integration: str = ""
    secret: bool = True


ENV_REGISTRY: dict[str, EnvSpec] = {
    # ----- Cloud providers (the engine and cloud_router read these
    # directly via os.environ — apply_aliases ensures non-canonical
    # casings populate the canonical names). -------------------------
    "OPENAI_API_KEY": EnvSpec(
        "OPENAI_API_KEY",
        ("OpenAI_API", "OPENAI_API"),
        "OpenAI Chat Completions API key",
        "openai",
    ),
    "ANTHROPIC_API_KEY": EnvSpec(
        "ANTHROPIC_API_KEY",
        (),
        "Anthropic Messages API key (direct, non-OpenRouter)",
        "anthropic",
    ),
    "ANTHROPIC_EMAIL": EnvSpec(
        "ANTHROPIC_EMAIL",
        (),
        "Email associated with the Anthropic account "
        "(used as identity metadata for login flows)",
        "anthropic",
        secret=False,
    ),
    "DEEPSEEK_API_KEY": EnvSpec(
        "DEEPSEEK_API_KEY",
        (),
        "DeepSeek chat/reasoner API key",
        "deepseek",
    ),
    # ----- Integrations --------------------------------------------
    "RAILWAY_TOKEN": EnvSpec(
        "RAILWAY_TOKEN",
        (),
        "Railway GraphQL API token (project/team-scoped)",
        "railway",
    ),
    "N8N_API_KEY": EnvSpec(
        "N8N_API_KEY",
        (),
        "n8n REST API key",
        "n8n",
    ),
    "N8N_BASE_URL": EnvSpec(
        "N8N_BASE_URL",
        (),
        "Base URL of the n8n instance "
        "(e.g. http://n8n.railway.internal:5678)",
        "n8n",
        secret=False,
    ),
    "SMTP_USER": EnvSpec(
        "SMTP_USER",
        (),
        "SMTP username/email",
        "email",
        secret=False,
    ),
    "SMTP_PASSWORD": EnvSpec(
        "SMTP_PASSWORD",
        (),
        "SMTP password or app-specific password",
        "email",
    ),
    "V0_API_KEY": EnvSpec(
        "V0_API_KEY",
        (),
        "V0 / Vercel UI generation API key",
        "v0",
    ),
    "GITHUB_PAT": EnvSpec(
        "GITHUB_PAT",
        ("GITHUB_TOKEN",),
        "GitHub personal access token (PAT) — falls back to GITHUB_TOKEN",
        "github",
    ),
    "CLOUDINARY_API_KEY": EnvSpec(
        "CLOUDINARY_API_KEY",
        (),
        "Cloudinary API key",
        "cloudinary",
    ),
    "CLOUDINARY_API_SECRET": EnvSpec(
        "CLOUDINARY_API_SECRET",
        (),
        "Cloudinary API secret",
        "cloudinary",
    ),
    "CLOUDINARY_CLOUD_NAME": EnvSpec(
        "CLOUDINARY_CLOUD_NAME",
        (),
        "Cloudinary cloud name (account identifier)",
        "cloudinary",
        secret=False,
    ),
    "DATABASE_URL": EnvSpec(
        "DATABASE_URL",
        (),
        "Postgres DSN (consumed by the elaboration store mirror)",
        "postgres",
    ),
    "OBSIDIAN_VAULT_URL": EnvSpec(
        "OBSIDIAN_VAULT_URL",
        (),
        "Base URL of the obsidian-vault MCP service "
        "(default http://obsidian-vault.railway.internal:22360)",
        "obsidian",
        secret=False,
    ),
    # ----- Payment processors -----------------------------------------
    "STRIPE_SECRET_KEY": EnvSpec(
        "STRIPE_SECRET_KEY",
        (),
        "Stripe API secret key (sk_live_... or sk_test_...). Read-only "
        "tools query revenue, charges, subscriptions, refunds; writes "
        "(create_refund) require model-side confirmation.",
        "stripe",
    ),
    "PAYPAL_CLIENT_ID": EnvSpec(
        "PAYPAL_CLIENT_ID",
        (),
        "PayPal REST app client id (paired with PAYPAL_CLIENT_SECRET).",
        "paypal",
        secret=False,
    ),
    "PAYPAL_CLIENT_SECRET": EnvSpec(
        "PAYPAL_CLIENT_SECRET",
        (),
        "PayPal REST app client secret. OAuth2 client-credentials flow "
        "exchanges this for a short-lived bearer token.",
        "paypal",
    ),
    "PAYPAL_API_BASE": EnvSpec(
        "PAYPAL_API_BASE",
        (),
        "PayPal API base URL — set to https://api-m.sandbox.paypal.com "
        "for sandbox testing. Defaults to live.",
        "paypal",
        secret=False,
    ),
    # ----- Google APIs (Calendar + Gmail share one OAuth client) -----
    # Calendar and Gmail are scopes on a SINGLE Google Cloud OAuth
    # client — the user does the OAuth dance once asking for both
    # calendar + gmail.modify scopes, captures one refresh_token, and
    # both connectors work. Aliases tolerate users who entered the
    # credentials under GMAIL_* (since they're enabling Gmail first):
    # those values flow through to GOOGLE_* via the alias pass on
    # startup, so no code change is needed elsewhere.
    "GOOGLE_CLIENT_ID": EnvSpec(
        "GOOGLE_CLIENT_ID",
        ("GMAIL_Client_ID", "GMAIL_CLIENT_ID"),
        "Google OAuth2 client id — shared by Calendar + Gmail. "
        "Create in Google Cloud Console -> APIs & Services -> "
        "Credentials. If you set GMAIL_Client_ID instead, it's read "
        "as an alias so the same value drives both connectors.",
        "google",
        secret=False,
    ),
    "GOOGLE_CLIENT_SECRET": EnvSpec(
        "GOOGLE_CLIENT_SECRET",
        ("GMAIL_Client_Secret", "GMAIL_CLIENT_SECRET"),
        "Google OAuth2 client secret (paired with GOOGLE_CLIENT_ID). "
        "GMAIL_Client_Secret resolves here via the alias pass.",
        "google",
    ),
    "GOOGLE_REFRESH_TOKEN": EnvSpec(
        "GOOGLE_REFRESH_TOKEN",
        (),
        "Long-lived refresh token from a one-time OAuth flow with "
        "scopes 'calendar' (and later 'gmail.modify'). Exchanged for "
        "short-lived access tokens on demand.",
        "google",
    ),
    # ----- BRIDGE Google account (second Gmail/Calendar) -------------
    # Multi-account: gmail_*/calendar_* tools accept account='bridge'
    # to act on these creds instead of the primary GOOGLE_* set.
    # Aliases include the user's existing Railway naming
    # (BRIDGE_GMAIL_Client_ID etc.) so it works without renaming.
    "BRIDGE_GOOGLE_CLIENT_ID": EnvSpec(
        "BRIDGE_GOOGLE_CLIENT_ID",
        (
            "BRIDGE_GMAIL_Client_ID",
            "BRIDGE_GMAIL_CLIENT_ID",
        ),
        "BRIDGE Google OAuth2 client id — second Google account, used "
        "when tools are called with account='bridge'.",
        "google_bridge",
        secret=False,
    ),
    "BRIDGE_GOOGLE_CLIENT_SECRET": EnvSpec(
        "BRIDGE_GOOGLE_CLIENT_SECRET",
        (
            "BRIDGE_GMAIL_Client_secret",
            "BRIDGE_GMAIL_Client_Secret",
            "BRIDGE_GMAIL_CLIENT_SECRET",
        ),
        "BRIDGE Google OAuth2 client secret.",
        "google_bridge",
    ),
    "BRIDGE_GOOGLE_REFRESH_TOKEN": EnvSpec(
        "BRIDGE_GOOGLE_REFRESH_TOKEN",
        (
            "BRIDGE_GMAIL_Refresh_Token",
            "BRIDGE_GMAIL_REFRESH_TOKEN",
        ),
        "BRIDGE Google long-lived refresh token. Capture via "
        "scripts/oauth_setup.py google --account=bridge.",
        "google_bridge",
    ),
    # ----- Microsoft Outlook (Microsoft Graph mail) -----------------
    # Aliases tolerate the space-separated names the user already
    # entered in Railway (e.g. "OUTLOOK_Client ID").
    "OUTLOOK_CLIENT_ID": EnvSpec(
        "OUTLOOK_CLIENT_ID",
        ("OUTLOOK_Client_ID", "OUTLOOK_Client ID"),
        "Microsoft / Azure AD app client id (Outlook OAuth2).",
        "outlook",
        secret=False,
    ),
    "OUTLOOK_CLIENT_SECRET": EnvSpec(
        "OUTLOOK_CLIENT_SECRET",
        ("OUTLOOK_Client_Secret",),
        "Microsoft / Azure AD app client secret (the VALUE column in "
        "Azure portal, not the Secret ID).",
        "outlook",
    ),
    "OUTLOOK_SECRET_ID": EnvSpec(
        "OUTLOOK_SECRET_ID",
        (),
        "Azure-side UUID identifying the client-secret object. "
        "Informational only — the OAuth runtime uses CLIENT_SECRET "
        "(the secret VALUE), not this id. Stored here so /v1/integrations/"
        "status can show whether you've recorded both fields from the "
        "Azure portal screen.",
        "outlook",
        secret=False,
    ),
    "OUTLOOK_REFRESH_TOKEN": EnvSpec(
        "OUTLOOK_REFRESH_TOKEN",
        ("OUTLOOK_Refresh_Token", "OUTLOOK_Refresh Token"),
        "Long-lived refresh token from a one-time Outlook OAuth flow "
        "with scopes Mail.ReadWrite + Mail.Send + offline_access.",
        "outlook",
    ),
    "OUTLOOK_TOKEN_URL": EnvSpec(
        "OUTLOOK_TOKEN_URL",
        ("OUTLOOK_Access_Token_URL", "OUTLOOK_Access Token URL"),
        "OAuth2 token endpoint. Defaults to Microsoft's common "
        "endpoint if unset.",
        "outlook",
        secret=False,
    ),
    "OUTLOOK_AUTH_URL": EnvSpec(
        "OUTLOOK_AUTH_URL",
        ("OUTLOOK_Authorization_URL",),
        "OAuth2 authorization endpoint (only needed when running the "
        "one-time browser OAuth flow).",
        "outlook",
        secret=False,
    ),
}


def get_env(*aliases: str, default: Optional[str] = None) -> Optional[str]:
    """Return the first non-empty ``os.environ`` value across ``aliases``.

    Case-sensitive: each alias is tried verbatim. For case-insensitive
    fallback, include the case variants explicitly.
    """
    for name in aliases:
        v = os.environ.get(name)
        if v:
            return v
    return default


def apply_aliases() -> list[str]:
    """Copy aliased values into their canonical env-var names if missing.

    Idempotent. Returns the list of canonical names that were populated
    from an alias.

    Runs once at module import (see ``openjarvis/core/__init__.py``) so
    that downstream code using raw ``os.environ.get(canonical)`` — and
    third-party SDKs like ``openai.OpenAI()`` that read
    ``OPENAI_API_KEY`` directly — work without modification when the
    user only set the alias.
    """
    populated: list[str] = []
    for spec in ENV_REGISTRY.values():
        if os.environ.get(spec.name):
            continue
        for alias in spec.aliases:
            v = os.environ.get(alias)
            if v:
                os.environ[spec.name] = v
                populated.append(spec.name)
                break
    return populated


def is_configured(canonical: str) -> bool:
    """Return True if env var (or any of its aliases) is set non-empty."""
    spec = ENV_REGISTRY.get(canonical)
    if spec is None:
        return bool(os.environ.get(canonical))
    return bool(get_env(spec.name, *spec.aliases))


__all__ = [
    "ENV_REGISTRY",
    "EnvSpec",
    "apply_aliases",
    "get_env",
    "is_configured",
]
