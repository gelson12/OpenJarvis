"""Permanent regression tests for the universal self-improvement loop.

Covers Round 8 (loop universality) + Round 9 (audit fixes).

Strategy: isolate each test to a tmp_path home, force-disable external
backends so PG/Vault/Mind don't get touched, exercise the local-only
behaviour with assertions. The mirror layers are tested separately for
their offline-degradation contract.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def loop_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point OPENJARVIS_HOME at a per-test tmp dir and disable every
    external mirror. Yields the home path. Forces a fresh module reload
    of every loop component so module-level state is clean."""
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OBSIDIAN_MIND_URL", raising=False)
    monkeypatch.delenv("OBSIDIAN_VAULT_URL", raising=False)
    monkeypatch.setenv("OPENJARVIS_MEMORY_WRITEBACK_ON_PROMOTION", "false")

    # Force re-import so module-level path resolution picks up the env
    import openjarvis.server.disavowal_detector as dd
    import openjarvis.server.learned_intents as li
    import openjarvis.server.learned_prompt_hints as ph
    import openjarvis.server.learning_pg as pg
    import openjarvis.server.learning_mind as mind
    for mod in (dd, li, ph, pg, mind):
        importlib.reload(mod)

    yield tmp_path


def test_disavowal_logged_with_domain(loop_env):
    """Round 8.1 — disavowal_detector logs ALL domains, not just email/calendar."""
    from openjarvis.server import disavowal_detector as dd

    ev = dd.record_if_disavowal(
        user_text="what year did the Roman Empire fall",
        assistant_text="I don't have a tool to look that up, sorry",
        session_id="t",
    )
    assert ev is not None
    assert ev["inferred_domain"] == "history"
    # Backward compat alias preserved
    assert "inferred_category" in ev


def test_disavowal_unrelated_text_returns_none(loop_env):
    """Anti-hallucination: harmless text must NOT be classified as a disavowal."""
    from openjarvis.server import disavowal_detector as dd

    ev = dd.record_if_disavowal(
        user_text="hello",
        assistant_text="Hi! How can I help?",
        session_id="t",
    )
    assert ev is None


def test_promotion_universal_across_non_tool_domains(loop_env):
    """Round 8.2 — clusters in history/code/etc promote with action=prompt_hint."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li

    samples = [
        "tell me about the byzantine empire collapse",
        "tell me about the byzantine empire history",
        "tell me about the byzantine empire dynasty",
    ]
    da = "I don't have a tool to look that historical fact up, sorry"
    for s in samples:
        dd.record_if_disavowal(user_text=s, assistant_text=da, session_id="t")

    promos = li.promote_from_disavowals()
    assert len(promos) >= 1
    history_promos = [p for p in promos if p["domain"] == "history"]
    assert len(history_promos) == 1
    p = history_promos[0]
    assert p["action"] == "prompt_hint"
    assert p["member_count"] == 3
    # Hint must contain a real fallback tactic (not boilerplate)
    assert any(
        k in (p.get("hint_text") or "")
        for k in ("web_search", "knowledge_search", "compute", "reason")
    )


def test_runtime_match_returns_hint_for_unseen_phrasing(loop_env):
    """Round 8.4 — match_learned must hit on a phrasing that wasn't in
    the training cluster but shares the discriminative tokens."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li

    for s in (
        "tell me about the byzantine empire collapse",
        "tell me about the byzantine empire history",
        "tell me about the byzantine empire dynasty",
    ):
        dd.record_if_disavowal(
            user_text=s,
            assistant_text="I don't have a tool to look that up, sorry",
            session_id="t",
        )
    li.promote_from_disavowals()

    # Unseen 4th phrasing — must trigger the same pattern.
    m = li.match_learned("tell me about the byzantine empire and its emperors")
    assert m is not None
    assert m["domain"] == "history"
    assert m["action"] == "prompt_hint"
    assert m.get("hint_text")


def test_anti_hallucination_unrelated_query(loop_env):
    """match_learned must NOT match an unrelated query after promotion."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li

    for s in (
        "tell me about the byzantine empire collapse",
        "tell me about the byzantine empire history",
        "tell me about the byzantine empire dynasty",
    ):
        dd.record_if_disavowal(
            user_text=s,
            assistant_text="I don't have a tool to look that up, sorry",
            session_id="t",
        )
    li.promote_from_disavowals()

    for unrelated in (
        "what time is it",
        "tell me a joke",
        "remind me to buy milk",
    ):
        assert li.match_learned(unrelated) is None


def test_lower_jaccard_catches_loose_phrasings(loop_env, monkeypatch):
    """Round 9.4 — default threshold 0.35 must let legitimate variants
    cluster that the old 0.5 would have dropped."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li

    # These three share only 2 of ~6 distinctive tokens (Jaccard ~0.33).
    # Round 9.4's default 0.35 should still catch them.
    samples = [
        "what year did the roman empire fall apart",
        "what year did the british empire collapse apart",
        "what year did the ottoman empire end apart",
    ]
    for s in samples:
        dd.record_if_disavowal(
            user_text=s,
            assistant_text="I don't have a tool to look that up, sorry",
            session_id="t",
        )
    promos = li.promote_from_disavowals()
    history_promos = [p for p in promos if p["domain"] == "history"]
    assert len(history_promos) >= 1, "0.35 Jaccard should catch this cluster"


def test_jaccard_threshold_env_overrides(loop_env, monkeypatch):
    """The threshold is configurable via env."""
    monkeypatch.setenv("OPENJARVIS_LEARNED_CLUSTER_JACCARD_MIN", "0.9")
    # Force re-import to pick up the env change
    import openjarvis.server.learned_intents as li
    importlib.reload(li)
    assert li._jaccard_min() == 0.9


def test_email_promotion_uses_preexec_action(loop_env):
    """Backward compat — email domain stays action=preexec."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li

    for s in (
        "check outlook inbox unread messages please",
        "check outlook inbox unread messages quickly",
        "check outlook inbox unread messages thoroughly",
    ):
        dd.record_if_disavowal(
            user_text=s,
            assistant_text="I don't have a tool to check your inbox, sorry",
            injected_tool_groups=["outlook"],
            session_id="t",
        )
    promos = li.promote_from_disavowals()
    email_p = [p for p in promos if p["domain"] == "email"]
    assert len(email_p) >= 1
    assert email_p[0]["action"] == "preexec"


def test_intent_preexec_injects_hint_for_learned_prompt_hint(loop_env):
    """Round 8.6 — intent_preexec must inject the hint as a context_block."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li
    from openjarvis.server import intent_preexec as ip

    for s in (
        "tell me about the byzantine empire collapse",
        "tell me about the byzantine empire history",
        "tell me about the byzantine empire dynasty",
    ):
        dd.record_if_disavowal(
            user_text=s,
            assistant_text="I don't have a tool to look that up, sorry",
            session_id="t",
        )
    li.promote_from_disavowals()

    ctx = ip.maybe_preexecute(
        "tell me about the byzantine empire and its emperors"
    )
    assert ctx is not None
    assert ctx.get("tool_name") is None  # No tool fired for prompt_hint
    assert "LEARNED PROMPT HINT" in (ctx.get("context_block") or "")


def test_calendar_broad_match_catches_separated_verb_and_noun():
    """Hotfix — the broad calendar regex must catch the user's real
    failing phrasing where 'look' and 'calendar' are 10 words apart."""
    from openjarvis.server.intent_preexec import (
        _detect_calendar_intent, _calendar_broad_match,
    )

    failing_phrase = (
        "I need you to look into my Outlook account and see if I have any "
        "scheduled for tomorrow in the calendar"
    )
    assert _calendar_broad_match(failing_phrase)
    det = _detect_calendar_intent(failing_phrase)
    assert det is not None
    assert det["provider"] == "outlook"
    assert det["window_label"] == "tomorrow"


def test_calendar_broad_match_rejects_false_positives():
    """The broad regex must NOT match unrelated 'my calendar' mentions."""
    from openjarvis.server.intent_preexec import _detect_calendar_intent

    for false_positive in (
        "the calendar app on my phone is broken",
        "tell me a joke",
        "what's the capital of france",
    ):
        assert _detect_calendar_intent(false_positive) is None


def test_past_window_detection():
    """Hotfix #2 — 'last meeting' / 'most recent' must resolve to a
    look-back window, not default-to-today."""
    from openjarvis.server.intent_preexec import (
        _detect_calendar_intent, _is_past_query, _resolve_window,
    )

    past_queries = [
        "verify when was the last meeting that I had",
        "what was the most recent meeting in my calendar",
        "check the last meeting I had this week",
    ]
    for q in past_queries:
        assert _is_past_query(q), f"_is_past_query missed: {q!r}"
        det = _detect_calendar_intent(q)
        assert det is not None
        assert "last" in det["window_label"] or "30" in det["window_label"]

    # Future-tense queries must NOT trip the past window
    future = "do I have any meetings tomorrow"
    assert not _is_past_query(future)


def test_persistence_survives_module_reload(loop_env):
    """Round 8 — patterns saved to JSON must reload after process restart."""
    from openjarvis.server import disavowal_detector as dd
    from openjarvis.server import learned_intents as li

    for s in (
        "tell me about the byzantine empire collapse",
        "tell me about the byzantine empire history",
        "tell me about the byzantine empire dynasty",
    ):
        dd.record_if_disavowal(
            user_text=s,
            assistant_text="I don't have a tool to look that up, sorry",
            session_id="t",
        )
    li.promote_from_disavowals()
    pre_match = li.match_learned("tell me about the byzantine empire and emperors")
    assert pre_match is not None

    # Simulate a fresh process
    li_reloaded = importlib.reload(li)
    post_match = li_reloaded.match_learned("tell me about the byzantine empire and emperors")
    assert post_match is not None
    assert post_match["domain"] == "history"


def test_mirrors_degrade_cleanly_when_offline(loop_env):
    """Round 9 — without DATABASE_URL / OBSIDIAN_MIND_URL, mirror calls
    must NOT raise."""
    from openjarvis.server import learning_pg as pg
    from openjarvis.server import learning_mind as mind

    assert pg.snapshot()["enabled"] is False
    assert mind.snapshot()["enabled"] is False
    # Calling them must not raise
    pg.mirror_disavowal({"ts": 1.0, "iso": "x", "inferred_domain": "test"})
    pg.mirror_pattern(domain="t", regex="x", action="prompt_hint",
                       hint_text=None, member_count=1, promoted_at="x")
    mind.index_disavowal({"ts": 1.0, "iso": "x", "inferred_domain": "test"})
    assert mind.semantic_cluster_candidates("test") == []


def test_snapshot_exposes_heartbeat(loop_env):
    """Round 9.2 — snapshot reports heartbeat info, observable from any process."""
    from openjarvis.server import learned_intents as li

    snap = li.snapshot()
    # Without a daemon running we expect heartbeat fields present but no data
    assert "daemon_alive_heartbeat" in snap
    assert "seconds_since_last_heartbeat" in snap
    assert "last_heartbeat_iso" in snap
    # Simulate a heartbeat write
    li._write_heartbeat()
    snap2 = li.snapshot()
    assert snap2["seconds_since_last_heartbeat"] is not None
    assert snap2["seconds_since_last_heartbeat"] < 5
    assert snap2["daemon_alive_heartbeat"] is True


def test_vault_slug_uses_sample_text_not_regex(loop_env, monkeypatch):
    """Round 9.1 — vault note filename must come from the sample user
    text, not the regex (which contains `\\b` anchors that leak as 'b')."""
    # We can't reach the actual vault, so we test the slug logic by
    # capturing the path that would be written.
    captured = {}

    class FakeClient:
        def write_file(self, path, content):
            captured["write_path"] = path

        def append_to_file(self, path, content):
            captured["append_path"] = path

    monkeypatch.setenv("OBSIDIAN_VAULT_URL", "http://stub")
    from openjarvis.integrations import obsidian_vault as ov_mod
    monkeypatch.setattr(ov_mod, "get_default_client", lambda: FakeClient())

    import openjarvis.server.learned_intents as li
    importlib.reload(li)

    promo = {
        "domain": "history",
        "pattern": r"^(?=.*\bbyzantine\b)(?=.*\bempire\b).+",
        "action": "prompt_hint",
        "hint_text": "test hint",
        "member_count": 3,
        "promoted_at": "2026-05-27T00:00:00+00:00",
        "sample_phrases": ["tell me about the byzantine empire collapse"],
    }
    li._vault_writeback(promo)

    written = captured.get("write_path", "")
    # The filename must contain "byzantine"/"empire" from the sample text,
    # and must NOT contain stray "-b-" or leading "b" artifacts from the
    # regex `\b` anchors.
    assert "byzantine" in written.lower()
    assert "-b-b" not in written  # the broken slug pattern
    assert not written.lower().endswith("-b.md")
