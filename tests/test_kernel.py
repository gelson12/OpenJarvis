"""Tests for the OpenJarvis Kernel — the deterministic capability layer.

These lock in the behaviours that the failing production conversation violated:
  * a real calendar query never disavows;
  * a tool ERROR is spoken as an error, NEVER as a fake "no meetings";
  * an empty calendar is EMPTY (honest "no meetings"), distinct from ERROR;
  * a non-data turn is PASSTHROUGH (handed to the LLM).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from openjarvis.kernel import calendar_capability as cal
from openjarvis.kernel import email_capability as eml
from openjarvis.kernel.contracts import Outcome, OutcomeStatus


# ── Parsing ──────────────────────────────────────────────────────────────

OUTLOOK_JSON = (
    '{"@odata.context":"x","value":['
    '{"subject":"Dentist","start":{"dateTime":"2026-05-30T14:30:00","timeZone":"UTC"}},'
    '{"subject":"Standup","start":{"dateTime":"2026-05-30T09:00:00"}}]}'
)
GOOGLE_JSON_WITH_NOTE = (
    "[NOTE: returned data is from the user's secondary Google account.]\n"
    '{"kind":"calendar#events","items":[{"summary":"Trip","start":{"date":"2026-05-30"}}]}'
)


def test_parse_outlook_events():
    ev = cal.parse_events(OUTLOOK_JSON)
    assert [e["title"] for e in ev] == ["Dentist", "Standup"]
    assert ev[0]["time"] == "14:30"


def test_parse_google_strips_note_prefix():
    ev = cal.parse_events(GOOGLE_JSON_WITH_NOTE)
    assert len(ev) == 1 and ev[0]["title"] == "Trip"


def test_parse_empty_calendar():
    assert cal.parse_events('{"value":[]}') == []


def test_parse_garbage_is_empty_not_crash():
    assert cal.parse_events("not json at all") == []


# ── Phrasing ─────────────────────────────────────────────────────────────

def test_phrase_zero():
    assert "no meetings" in cal._phrase([], "tomorrow")


def test_phrase_counts():
    one = cal._phrase([{"title": "A", "time": ""}], "today")
    assert one.startswith("You have one meeting")
    many = cal._phrase([{"title": str(i), "time": ""} for i in range(5)], "today")
    assert "5 meetings" in many and "2 more" in many


# ── resolve(): success / empty / error are all distinct ───────────────────

def test_resolve_passthrough_for_non_calendar():
    assert cal.resolve("what's the weather like").is_passthrough


def test_resolve_ok_from_real_events(monkeypatch):
    monkeypatch.setattr(cal, "_run_tool", lambda *a, **k: (True, OUTLOOK_JSON))
    out = cal.resolve("do I have any meetings tomorrow on my outlook calendar")
    assert out.status is OutcomeStatus.OK
    assert "Dentist" in out.message
    assert out.data["count"] == 2


def test_resolve_empty_when_zero_events(monkeypatch):
    monkeypatch.setattr(cal, "_run_tool", lambda *a, **k: (True, '{"value":[]}'))
    out = cal.resolve("any meetings tomorrow on outlook")
    assert out.status is OutcomeStatus.EMPTY
    assert "no meetings" in out.message.lower()


def test_resolve_error_is_never_empty(monkeypatch):
    """The exact production bug: a failed fetch must NOT read as 'no meetings'."""
    monkeypatch.setattr(cal, "_run_tool", lambda *a, **k: (False, "outlook error: token expired"))
    out = cal.resolve("any meetings tomorrow on my outlook calendar")
    assert out.status is OutcomeStatus.ERROR
    assert "no meetings" not in out.message.lower()
    assert "couldn't reach" in out.message.lower()


def test_resolve_never_disavows(monkeypatch):
    """Whatever happens, the kernel never claims the capability is missing."""
    for ok, payload in [(True, OUTLOOK_JSON), (True, '{"value":[]}'),
                        (False, "boom")]:
        monkeypatch.setattr(cal, "_run_tool", lambda *a, _o=ok, _p=payload, **k: (_o, _p))
        out = cal.resolve("check my outlook calendar tomorrow")
        assert "don't have" not in out.message.lower()
        assert "do not have" not in out.message.lower()
        assert "can't access" not in out.message.lower()


# ── email ─────────────────────────────────────────────────────────────────

def test_email_error_is_honest(monkeypatch):
    monkeypatch.setattr(eml, "_run_tool", lambda *a, **k: (False, "outlook error: 401"))
    out = eml.resolve("any unread emails in my outlook inbox")
    assert out.status is OutcomeStatus.ERROR
    assert "couldn't reach" in out.message.lower()


def test_email_ok(monkeypatch):
    payload = '{"value":[{"subject":"Invoice","from":{"emailAddress":{"name":"Pedro"}}}]}'
    monkeypatch.setattr(eml, "_run_tool", lambda *a, **k: (True, payload))
    out = eml.resolve("any new emails in outlook")
    assert out.status is OutcomeStatus.OK
    assert "Pedro" in out.message


# ── Outcome contract ────────────────────────────────────────────────────────

def test_outcome_predicates():
    assert Outcome.passthrough().is_passthrough
    assert Outcome.ok("hi").is_final
    assert Outcome.error("no").is_final
    assert not Outcome.passthrough().is_final
