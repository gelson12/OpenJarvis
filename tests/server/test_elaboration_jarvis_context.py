"""Tests for the Jarvis-context system note prepended to Claude-CLI prompts.

Verifies the note correctly names the user's real n8n URL (not a stale
MCP server endpoint), conditionally lists only the configured
integrations, and is always prepended to the elaboration prompt.
"""

from __future__ import annotations

import pytest

from openjarvis.server.claude_cli_client import (
    _build_prompt,
    _jarvis_context_note,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with an empty integration env so we can assert
    only the integrations we explicitly set show up in the note."""
    for k in (
        "N8N_BASE_URL",
        "N8N_API_KEY",
        "STRIPE_SECRET_KEY",
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GITHUB_PAT",
        "GITHUB_TOKEN",
        "OBSIDIAN_VAULT_URL",
        "CLOUDINARY_API_KEY",
        "V0_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


def test_note_with_no_integrations_still_has_disclaimer(monkeypatch):
    note = _jarvis_context_note()
    assert "[JARVIS-CONTEXT]" in note
    assert "do not paste curl" in note
    assert "MCP server endpoints" in note


def test_note_includes_real_n8n_url_when_configured(monkeypatch):
    monkeypatch.setenv("N8N_BASE_URL", "https://my-real-n8n.example.com/")
    note = _jarvis_context_note()
    # Trailing slash should be stripped
    assert "https://my-real-n8n.example.com" in note
    # Should mention the credentials API too
    assert "n8n_list_credentials" in note
    assert "Slack, Gmail OAuth, Stripe, Notion" in note


def test_note_lists_only_configured_integrations(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    note = _jarvis_context_note()
    assert "stripe" in note
    assert "google calendar" in note
    # Not configured -> not mentioned
    assert "paypal" not in note
    assert "github" not in note
    assert "obsidian" not in note


def test_paypal_only_mentioned_when_both_creds_set(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    note = _jarvis_context_note()
    assert "paypal" not in note
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")
    note2 = _jarvis_context_note()
    assert "paypal" in note2


def test_build_prompt_prepends_jarvis_context():
    """Every elaboration submitted to Claude-CLI gets the context note
    prepended — even if the user's messages don't mention n8n at all."""
    messages = [{"role": "user", "content": "what's 2+2?"}]
    prompt = _build_prompt(messages)
    assert prompt.startswith("[JARVIS-CONTEXT]")
    assert "[USER]" in prompt
    assert "what's 2+2?" in prompt


def test_build_prompt_preserves_message_order(monkeypatch):
    monkeypatch.setenv("N8N_BASE_URL", "https://n8n.example.com")
    messages = [
        {"role": "system", "content": "you are jarvis"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "list workflows"},
    ]
    prompt = _build_prompt(messages)
    # Jarvis-context first, then messages in order.
    sections = prompt.split("\n\n")
    assert sections[0].startswith("[JARVIS-CONTEXT]")
    assert sections[1].startswith("[SYSTEM]")
    assert sections[2].startswith("[USER]")
    assert sections[3].startswith("[ASSISTANT]")
    assert sections[4].startswith("[USER]")
