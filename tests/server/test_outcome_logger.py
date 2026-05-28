"""Round 20 Piece 4 — tests for outcome logging + tool-affinity rewards.

Covers:
  * record_turn writes JSONL with the expected schema.
  * read_recent returns the last N records.
  * Disabled flag short-circuits without writing.
  * Affinity computation: success rates per (query_bucket, tool).
  * affinity_bias_for_tool returns positive bias for high-success
    pairs, negative for low-success, 0 for unknowns.
  * Query bucketing strips filler and uses the first content word.
  * snapshot() returns the expected keys and the recent-stats summary.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def outcomes_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("OPENJARVIS_OUTCOME_LOGGING_ENABLED", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import openjarvis.server.outcome_logger as ol
    importlib.reload(ol)
    yield tmp_path


def test_record_turn_writes_jsonl(outcomes_env):
    from openjarvis.server import outcome_logger as ol
    ol.record_turn(
        query="check my calendar",
        tools_offered=["calendar_list_events", "outlook_list_messages"],
        tool_called="calendar_list_events",
        tool_success=True,
        latency_ms=420,
        session_id="t1",
    )
    p = outcomes_env / "outcomes.jsonl"
    assert p.exists()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["query"] == "check my calendar"
    assert rec["tool_called"] == "calendar_list_events"
    assert rec["tool_success"] is True
    assert rec["latency_ms"] == 420


def test_disabled_does_not_write(outcomes_env, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_OUTCOME_LOGGING_ENABLED", "false")
    import openjarvis.server.outcome_logger as ol
    importlib.reload(ol)
    ol.record_turn(query="anything", tool_called="x", tool_success=True)
    p = outcomes_env / "outcomes.jsonl"
    assert not p.exists()


def test_empty_query_ignored(outcomes_env):
    from openjarvis.server import outcome_logger as ol
    ol.record_turn(query="", tool_called="x", tool_success=True)
    p = outcomes_env / "outcomes.jsonl"
    assert not p.exists()


def test_read_recent(outcomes_env):
    from openjarvis.server import outcome_logger as ol
    for i in range(5):
        ol.record_turn(query=f"query {i}", tool_called="tool_a", tool_success=True)
    recs = ol.read_recent(limit=3)
    assert len(recs) == 3
    # Should be the LAST 3
    assert recs[-1]["query"] == "query 4"


def test_query_bucketing_strips_filler(outcomes_env):
    from openjarvis.server.outcome_logger import _bucket_query
    # Generic openers + stop-words should be stripped, the first
    # CONTENT word wins. Bucketing is approximate — it groups queries
    # that start with the same content token, not a "topic" oracle.
    assert _bucket_query("Hey Jarvis, can you check my calendar?") == "calendar"
    assert _bucket_query("show me the news") == "news"
    # "send" is the first content token after stripping "I want to" —
    # accurate to behaviour even though the topic is "email".
    assert _bucket_query("I want to send an email") == "send"
    # Edge cases
    assert _bucket_query("") == "_unknown"
    assert _bucket_query("hi") == "_unknown"


def test_affinity_computation(outcomes_env):
    """3+ observations of (bucket, tool) -> success rate appears."""
    from openjarvis.server import outcome_logger as ol
    # calendar bucket: 4/5 success for calendar_list_events
    for ok in (True, True, True, True, False):
        ol.record_turn(
            query="check my calendar today",
            tool_called="calendar_list_events",
            tool_success=ok,
        )
    # emails bucket (literal first content word): 1/3 success
    for ok in (True, False, False):
        ol.record_turn(
            query="show me emails",
            tool_called="outlook_list_messages",
            tool_success=ok,
        )
    # below-threshold case (2 obs) — should NOT appear in the map
    for ok in (True, True):
        ol.record_turn(
            query="weather please",
            tool_called="weather_get",
            tool_success=ok,
        )
    m = ol.compute_affinity_map(min_observations=3)
    assert "calendar" in m
    assert "emails" in m  # plural — bucket = first content token literally
    assert "weather" not in m
    assert m["calendar"]["calendar_list_events"] == 0.8
    assert m["emails"]["outlook_list_messages"] == round(1 / 3, 3)


def test_affinity_bias_for_tool(outcomes_env):
    from openjarvis.server import outcome_logger as ol
    # Pre-load the affinity map via the save API
    ol.save_affinity_map({
        "calendar": {
            "calendar_list_events": 0.9,
            "web_search": 0.1,
        },
    })
    # High-success tool -> positive bias
    bias_good = ol.affinity_bias_for_tool("check my calendar", "calendar_list_events")
    assert bias_good > 0.5
    # Low-success tool -> negative bias
    bias_bad = ol.affinity_bias_for_tool("check my calendar", "web_search")
    assert bias_bad < -0.5
    # Unknown tool/bucket -> 0
    assert ol.affinity_bias_for_tool("check my calendar", "unknown_tool") == 0.0
    assert ol.affinity_bias_for_tool("entirely new query", "anything") == 0.0


def test_snapshot_fields(outcomes_env):
    from openjarvis.server import outcome_logger as ol
    ol.record_turn(query="hello", tool_called="t1", tool_success=True)
    ol.record_turn(query="hello", tool_called="t1", llm_disavowed=True)
    snap = ol.snapshot()
    expected_keys = {
        "enabled", "interval_sec", "affinity_weight", "total_turns_logged",
        "tool_affinity_clusters", "tool_call_counts_top10",
        "successful_tool_calls", "disavowals_logged",
        "affinity_map_size_bytes", "log_path",
    }
    assert expected_keys <= set(snap.keys())
    assert snap["total_turns_logged"] == 2
    assert snap["successful_tool_calls"] == 1
    assert snap["disavowals_logged"] == 1
    assert snap["tool_call_counts_top10"]["t1"] == 2


def test_disabled_bias_returns_zero(outcomes_env, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_OUTCOME_LOGGING_ENABLED", "false")
    import openjarvis.server.outcome_logger as ol
    importlib.reload(ol)
    assert ol.affinity_bias_for_tool("any query", "any_tool") == 0.0
