#!/usr/bin/env python3
"""One-shot OAuth helper — run on your laptop to capture a refresh token.

The OpenJarvis Google + Outlook integrations need a long-lived
refresh_token in env. There's no way to get one without a browser
consent flow at least once. This script automates it:

  1. You provide CLIENT_ID + CLIENT_SECRET (and provider: google or outlook).
  2. Script opens your default browser to the consent URL.
  3. You sign in to the account you want OpenJarvis to act on, click
     "Allow" on the scopes shown.
  4. Browser redirects to http://localhost:8080/oauth2callback?code=...
  5. Script catches the code, exchanges it for the refresh_token, and
     prints it. Copy that into Railway as GOOGLE_REFRESH_TOKEN or
     OUTLOOK_REFRESH_TOKEN.

Prerequisites:
  - Python 3.10+ (stdlib only — no pip install needed beyond `httpx`
    which is already in the OpenJarvis dev env)
  - In Google Cloud Console / Azure portal, your OAuth client must have
    http://localhost:8080/oauth2callback in its authorized redirect URIs.

Usage examples:
  python scripts/oauth_setup.py google \\
      --client-id=YOUR_GOOGLE_CLIENT_ID \\
      --client-secret=YOUR_GOOGLE_CLIENT_SECRET

  python scripts/oauth_setup.py outlook \\
      --client-id=YOUR_OUTLOOK_CLIENT_ID \\
      --client-secret=YOUR_OUTLOOK_CLIENT_SECRET
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from typing import Optional

import httpx

REDIRECT_URI = "http://localhost:8080/oauth2callback"
REDIRECT_PORT = 8080


# Provider-specific endpoints + scopes
PROVIDERS = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # calendar (full read/write) + gmail.modify (read + send + label)
        "scope": (
            "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/gmail.modify"
        ),
        "extra_auth_params": {
            "access_type": "offline",
            "prompt": "consent",  # force re-consent to ensure refresh_token returns
        },
        "env_var": "GOOGLE_REFRESH_TOKEN",
    },
    "outlook": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Mail.ReadWrite + Mail.Send + offline_access (refresh_token) + User.Read (profile)
        "scope": "Mail.ReadWrite Mail.Send offline_access User.Read",
        "extra_auth_params": {
            "response_mode": "query",
        },
        "env_var": "OUTLOOK_REFRESH_TOKEN",
    },
}


_received_code: Optional[str] = None
_received_state: Optional[str] = None
_callback_event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the redirect from the provider with ?code=... in the URL."""

    def do_GET(self) -> None:  # noqa: N802 — http.server convention
        global _received_code, _received_state

        if not self.path.startswith("/oauth2callback"):
            self.send_response(404)
            self.end_headers()
            return

        qs = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(qs))
        _received_code = params.get("code")
        _received_state = params.get("state")
        error = params.get("error")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if error:
            body = (
                f"<h1>OAuth error</h1><p>{error}: {params.get('error_description', '')}</p>"
                "<p>You can close this tab and re-run the script.</p>"
            )
        elif _received_code:
            body = (
                "<h1>✓ Got the code!</h1>"
                "<p>You can close this tab. Check the terminal "
                "for your refresh token.</p>"
            )
        else:
            body = "<h1>Unexpected callback</h1>"
        self.wfile.write(body.encode("utf-8"))
        _callback_event.set()

    def log_message(self, format: str, *args) -> None:
        # Silence the default access-log spam.
        pass


def _start_callback_server() -> socketserver.TCPServer:
    server = socketserver.TCPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _build_auth_url(provider: dict, client_id: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": provider["scope"],
        "state": state,
        **provider["extra_auth_params"],
    }
    return f"{provider['auth_url']}?{urllib.parse.urlencode(params)}"


def _exchange_code_for_token(
    provider: dict,
    client_id: str,
    client_secret: str,
    code: str,
) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if provider["env_var"].startswith("OUTLOOK"):
        data["scope"] = provider["scope"]  # Microsoft requires scope on token exchange too
    resp = httpx.post(provider["token_url"], data=data, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture an OAuth2 refresh_token for OpenJarvis.",
    )
    parser.add_argument("provider", choices=sorted(PROVIDERS.keys()))
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument(
        "--account",
        default="primary",
        help=(
            "Account name (e.g. 'primary', 'bridge'). Determines the env "
            "var the script tells you to set: GOOGLE_REFRESH_TOKEN for "
            "primary, <ACCOUNT>_GOOGLE_REFRESH_TOKEN for any other. "
            "Outlook ignores this — it's single-account today."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser; just print the auth URL.",
    )
    args = parser.parse_args()

    provider = PROVIDERS[args.provider]
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(provider, args.client_id, state)

    print(f"\n=== {args.provider.upper()} OAuth setup ===")
    print(f"Listening for the callback on {REDIRECT_URI} ...")

    server = _start_callback_server()
    try:
        if args.no_browser:
            print(f"\nOpen this URL in your browser:\n  {auth_url}\n")
        else:
            print(f"\nOpening browser to:\n  {auth_url}\n")
            webbrowser.open(auth_url)

        # Wait up to 5 minutes for the user to complete the consent flow.
        if not _callback_event.wait(timeout=300):
            print("\n✗ Timed out waiting for callback (5 min).", file=sys.stderr)
            return 1

        if _received_state != state:
            print(
                f"\n✗ State mismatch — possible CSRF attempt. "
                f"Expected {state!r}, got {_received_state!r}",
                file=sys.stderr,
            )
            return 1
        if not _received_code:
            print("\n✗ No code received — see the browser tab for details.", file=sys.stderr)
            return 1

        print("\n✓ Got authorization code; exchanging for refresh token...")
        try:
            payload = _exchange_code_for_token(
                provider, args.client_id, args.client_secret, _received_code,
            )
        except httpx.HTTPStatusError as exc:
            print(
                f"\n✗ Token exchange failed: HTTP {exc.response.status_code}\n"
                f"   Body: {exc.response.text}",
                file=sys.stderr,
            )
            return 1

        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            print(
                "\n✗ Provider returned no refresh_token. Common causes:\n"
                "  • For Google: you've already granted this client; revoke at "
                "https://myaccount.google.com/permissions and re-run.\n"
                "  • Make sure the OAuth client type is 'Web application'.\n"
                f"  Full response: {payload}",
                file=sys.stderr,
            )
            return 1

        # Compute the right env-var name for this account. Primary uses
        # the canonical names (GOOGLE_REFRESH_TOKEN). Any other account
        # gets a prefix (e.g. BRIDGE_GOOGLE_REFRESH_TOKEN). Outlook is
        # single-account so always uses OUTLOOK_REFRESH_TOKEN.
        env_var = provider["env_var"]
        if args.provider == "google" and args.account != "primary":
            env_var = f"{args.account.upper()}_GOOGLE_REFRESH_TOKEN"

        print("\n" + "=" * 70)
        print("✅ SUCCESS — set this in Railway:")
        print()
        print(f"  {env_var} = {refresh_token}")
        print()
        if args.provider == "google" and args.account != "primary":
            print(
                f"Account: {args.account!r} (call gmail_*/calendar_* tools "
                f"with account='{args.account}' to use this)"
            )
        print(f"Token type: {payload.get('token_type', '?')}")
        print(f"Access token expires in: {payload.get('expires_in', '?')} s")
        scopes = payload.get("scope", "")
        if scopes:
            print(f"Granted scopes: {scopes}")
        print("=" * 70)
        print()
        print("Don't share this refresh_token. Anyone with it can act on")
        print("the account you just authorized.")
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
