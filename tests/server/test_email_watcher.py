"""Regression tests for the email watcher (sender, topic, junkmail).

Covers the production-reported failure: user said "let me know if an
email about X arrives" / "notify me if anything lands in junk mail" and
nothing happened. The watcher only knew "from <Name>" patterns.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def watch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    import openjarvis.server.email_watcher as ew
    importlib.reload(ew)
    yield tmp_path


# ---------- Intent detection ----------

def test_sender_only_phrasings_still_work():
    from openjarvis.server.email_watcher import detect_watch_intent

    cases = [
        ("let me know when Pedro emails me", "Pedro"),
        ("notify me if I get an email from John", "John"),
        ("watch my inbox for emails from sarah@example.com", "sarah@example.com"),
        ("if Anna emails me", "Anna"),
    ]
    for text, expected_sender in cases:
        intent = detect_watch_intent(text)
        assert intent is not None, f"missed: {text!r}"
        assert expected_sender.lower() in (intent.get("sender") or "").lower()


def test_all_caps_acronym_sender():
    """'from VIQU' was previously rejected because the regex required
    proper-case names."""
    from openjarvis.server.email_watcher import detect_watch_intent

    intent = detect_watch_intent("wait for an email from VIQU about the contract")
    assert intent is not None
    assert intent["sender"] == "VIQU"


def test_topic_only_watch():
    """'let me know if I get an email about X' must create a topic-only
    watch (no sender required)."""
    from openjarvis.server.email_watcher import detect_watch_intent

    cases = [
        ("let me know if I get an email about the interview", "interview"),
        ("alert me if any email mentions VIQU", "VIQU"),
        ("notify me when an email with subject 'Phone Interview' arrives",
         "Phone Interview"),
        ("tell me when an email regarding the contract arrives", "contract"),
        ("let me know if anything about screening comes in", "screening"),
    ]
    for text, expected_topic_fragment in cases:
        intent = detect_watch_intent(text)
        assert intent is not None, f"missed: {text!r}"
        subj = (intent.get("subject_contains") or "").lower()
        assert expected_topic_fragment.lower() in subj, (
            f"{text!r}: expected {expected_topic_fragment!r} in subject "
            f"{subj!r}"
        )


def test_sender_AND_topic_combined():
    """'let me know when Pedro emails me about the contract' should
    capture BOTH sender and subject."""
    from openjarvis.server.email_watcher import detect_watch_intent

    intent = detect_watch_intent(
        "let me know when Pedro emails me about the contract"
    )
    assert intent is not None
    assert intent["sender"] == "Pedro"
    assert "contract" in (intent["subject_contains"] or "").lower()


def test_junkmail_folder_detection():
    """Mentions of 'junk' or 'spam' must add 'junkemail' to folders."""
    from openjarvis.server.email_watcher import detect_watch_intent

    intent = detect_watch_intent(
        "notify me if an email from John lands in my junk mail"
    )
    assert intent is not None
    assert "junkemail" in (intent.get("folders") or [])

    # Pure inbox phrasing must NOT include junkemail
    intent2 = detect_watch_intent("let me know when Pedro emails me")
    assert intent2.get("folders") == ["inbox"]


def test_both_inbox_and_junkmail():
    from openjarvis.server.email_watcher import detect_watch_intent

    intent = detect_watch_intent(
        "let me know if anything about VIQU lands in my inbox or junk mail"
    )
    assert intent is not None
    folders = set(intent.get("folders") or [])
    assert "inbox" in folders
    assert "junkemail" in folders


def test_wait_for_email_phrasing():
    """The verb 'wait for' previously consumed the only 'for' connector
    so the regex would miss 'wait for an email from VIQU'."""
    from openjarvis.server.email_watcher import detect_watch_intent

    intent = detect_watch_intent("wait for an email from VIQU")
    assert intent is not None
    assert intent["sender"] == "VIQU"


def test_unrelated_text_returns_none():
    from openjarvis.server.email_watcher import detect_watch_intent

    for text in (
        "what's the weather like",
        "tell me a joke",
        "send Pedro an email saying hi",  # this is "send", not "watch for"
    ):
        assert detect_watch_intent(text) is None, f"false positive: {text!r}"


# ---------- Storage ----------

def test_topic_only_watch_creates_and_lists(watch_env):
    """add_watch must accept topic-only (no sender) and persist."""
    from openjarvis.server import email_watcher as ew

    wid = ew.add_watch(sender="", subject_contains="interview",
                       folders=["inbox", "junkemail"])
    assert wid
    watches = ew.list_watches(active_only=True)
    assert len(watches) == 1
    w = watches[0]
    assert w["sender"] == ""
    assert w["subject_contains"] == "interview"
    assert w["folders"] == ["inbox", "junkemail"]


def test_invalid_watch_rejected(watch_env):
    """A watch with neither sender nor subject must be rejected."""
    from openjarvis.server import email_watcher as ew

    assert ew.add_watch(sender="", subject_contains="") is None


def test_watch_folders_default_to_inbox(watch_env):
    from openjarvis.server import email_watcher as ew

    wid = ew.add_watch(sender="Pedro")
    assert wid
    w = ew.list_watches()[0]
    assert w["folders"] == ["inbox"]


def test_watch_folders_sanitized(watch_env):
    """Garbage folder names must be dropped, defaulting back to inbox."""
    from openjarvis.server import email_watcher as ew

    wid = ew.add_watch(sender="Pedro", folders=["inbox", "bogus", "JUNKEMAIL"])
    w = ew.list_watches()[0]
    assert "inbox" in w["folders"]
    assert "junkemail" in w["folders"]
    assert "bogus" not in w["folders"]


def test_distinct_watches_get_distinct_ids(watch_env):
    """Same sender but different topic/folders must NOT collide on id."""
    from openjarvis.server import email_watcher as ew

    w1 = ew.add_watch(sender="Pedro", subject_contains="invoice")
    w2 = ew.add_watch(sender="Pedro", subject_contains="contract")
    w3 = ew.add_watch(sender="Pedro", subject_contains="invoice",
                       folders=["junkemail"])
    assert w1 and w2 and w3
    assert len({w1, w2, w3}) == 3
