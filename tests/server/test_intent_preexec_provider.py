"""Round 10.7 — regression tests for provider routing in intent_preexec.

The production failure: user said "Do I have any meetings on my Gmail
calendar?" and Jarvis returned Outlook results. The cause: when the
semantic turn-detector split the utterance into fragments, the
fragment containing "Gmail" was processed independently of the
fragment containing "calendar", so the provider regex never saw the
hint and silently defaulted to Outlook.

This suite verifies:
  - Explicit Gmail/Google mentions route to Google.
  - Explicit Outlook mentions route to Outlook.
  - Ambiguous current message + Gmail in history -> Google.
  - Ambiguous current + nothing in history -> default Outlook BUT with
    provider_assumed=True so the LLM knows to switch on a follow-up.
"""

from __future__ import annotations

import pytest

from openjarvis.server.intent_preexec import (
    _detect_calendar_intent,
    _detect_email_intent,
    _detect_provider,
)


# ---------- Low-level _detect_provider ----------

class TestDetectProvider:
    def test_explicit_outlook(self):
        provider, assumed = _detect_provider("check my outlook calendar")
        assert provider == "outlook"
        assert assumed is False

    def test_explicit_gmail(self):
        provider, assumed = _detect_provider("check my gmail")
        assert provider == "google"
        assert assumed is False

    def test_explicit_google(self):
        provider, assumed = _detect_provider("any events on google calendar")
        assert provider == "google"
        assert assumed is False

    def test_history_gmail_with_ambiguous_current(self):
        """Prior turn mentioned Gmail -> current ambiguous turn inherits."""
        provider, assumed = _detect_provider(
            "any meetings today",
            history_texts=["I want to check my gmail later"],
        )
        assert provider == "google"
        assert assumed is False

    def test_history_outlook_with_ambiguous_current(self):
        provider, assumed = _detect_provider(
            "any meetings today",
            history_texts=["look at my outlook"],
        )
        assert provider == "outlook"
        assert assumed is False

    def test_neither_falls_to_default_with_assumed_flag(self):
        provider, assumed = _detect_provider("any meetings today")
        assert provider == "outlook"
        assert assumed is True

    def test_current_message_beats_history(self):
        """Even if history has gmail, an explicit outlook in current wins."""
        provider, assumed = _detect_provider(
            "show my outlook calendar",
            history_texts=["i love my gmail"],
        )
        assert provider == "outlook"
        assert assumed is False

    def test_gmail_label_param_for_email(self):
        """Email path uses 'gmail' as the Google label (not 'google')."""
        provider, assumed = _detect_provider(
            "check my gmail",
            google_label="gmail",
        )
        assert provider == "gmail"
        assert assumed is False


# ---------- _detect_calendar_intent ----------

class TestCalendarIntent:
    def test_gmail_calendar_routes_to_google(self):
        result = _detect_calendar_intent(
            "do I have any meetings on my Gmail calendar?"
        )
        assert result is not None
        assert result["provider"] == "google"
        assert result["provider_assumed"] is False

    def test_outlook_calendar_routes_to_outlook(self):
        result = _detect_calendar_intent(
            "check my outlook calendar for today"
        )
        assert result is not None
        assert result["provider"] == "outlook"
        assert result["provider_assumed"] is False

    def test_ambiguous_calendar_defaults_outlook_with_flag(self):
        result = _detect_calendar_intent("do I have any meetings tomorrow")
        assert result is not None
        assert result["provider"] == "outlook"
        assert result["provider_assumed"] is True

    def test_ambiguous_calendar_with_gmail_history_routes_google(self):
        """The production bug: Gmail in one fragment, calendar query in next."""
        result = _detect_calendar_intent(
            "do I have any meetings today",
            history_texts=[
                "I want to switch to using my gmail account",
                "hold on a moment",
            ],
        )
        assert result is not None
        assert result["provider"] == "google"
        assert result["provider_assumed"] is False


# ---------- _detect_email_intent ----------

class TestEmailIntent:
    def test_gmail_inbox_routes_to_gmail_label(self):
        """Email path uses 'gmail' (not 'google') as the provider label
        so _run_gmail_list_messages is selected."""
        result = _detect_email_intent("check my gmail inbox")
        assert result is not None
        assert result["provider"] == "gmail"
        assert result["provider_assumed"] is False

    def test_outlook_inbox_routes_to_outlook(self):
        result = _detect_email_intent("check my outlook inbox")
        assert result is not None
        assert result["provider"] == "outlook"
        assert result["provider_assumed"] is False

    def test_ambiguous_email_defaults_outlook_with_flag(self):
        result = _detect_email_intent("any unread emails")
        assert result is not None
        assert result["provider"] == "outlook"
        assert result["provider_assumed"] is True

    def test_ambiguous_email_with_gmail_history(self):
        result = _detect_email_intent(
            "any unread messages",
            history_texts=["my gmail has been very busy"],
        )
        assert result is not None
        assert result["provider"] == "gmail"
        assert result["provider_assumed"] is False
