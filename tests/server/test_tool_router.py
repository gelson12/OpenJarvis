"""Round 20 Piece 1 — tests for the embedding-based tool router.

Strategy: mock the embedder so the test doesn't require sentence-
transformers to be installed and doesn't pay a model-loading penalty.
Cover:
  * Empty / disabled cases return [].
  * Embedding cache key changes when registry changes.
  * Cosine ranking returns tools in expected order.
  * Score threshold filters out low-relevance tools.
  * build_tool_hint_block produces a usable system message.
  * snapshot() returns the expected fields.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest


@pytest.fixture
def router_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Per-test isolation: tmp OPENJARVIS_HOME + fresh module reload so
    the embedder + corpus caches are pristine."""
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("OPENJARVIS_TOOL_ROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENJARVIS_TOOL_ROUTER_TOP_K", "5")
    monkeypatch.setenv("OPENJARVIS_TOOL_ROUTER_MIN_SCORE", "0.0")
    import openjarvis.server.tool_router as tr
    importlib.reload(tr)
    yield tmp_path


class _FakeEmbedder:
    """Deterministic stub embedder: hashes tokens into a small vector
    so semantically-similar phrases share dimensions. Uses hashlib for
    cross-process determinism — Python's built-in hash() is randomised
    per process. Good enough for unit-testing ranking without the real
    model."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def dim(self) -> int:
        return self._dim

    @staticmethod
    def _hash_tok(tok: str, mod: int) -> int:
        import hashlib
        return int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % mod

    def embed(self, texts: List[str]) -> Any:
        import re
        import numpy as np
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, t in enumerate(texts):
            # Split on non-word characters so "tool_list_events." and
            # "tool list events" both tokenize the same way.
            for tok in re.findall(r"[a-z0-9]+", (t or "").lower()):
                bucket = self._hash_tok(tok, self._dim)
                out[i, bucket] += 1.0
        # L2-normalise
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return out / norms


def test_disabled_returns_empty(router_env, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_TOOL_ROUTER_ENABLED", "false")
    import openjarvis.server.tool_router as tr
    importlib.reload(tr)
    assert tr.rank_tools_for_query("anything") == []
    assert tr.build_tool_hint_block("anything") is None


def test_no_query_returns_empty(router_env):
    import openjarvis.server.tool_router as tr
    assert tr.rank_tools_for_query("") == []
    assert tr.rank_tools_for_query(None) == []  # type: ignore[arg-type]


def test_embedder_unavailable_degrades(router_env, monkeypatch):
    """When sentence-transformers can't load, router silently returns []."""
    import openjarvis.server.tool_router as tr
    monkeypatch.setattr(tr, "_get_embedder", lambda: None)
    assert tr.rank_tools_for_query("anything") == []
    assert tr.build_tool_hint_block("anything") is None


def test_ranking_finds_relevant_tools(router_env, monkeypatch):
    """With the fake embedder, query 'calendar meeting' should rank
    tools whose descriptions contain those tokens above unrelated ones."""
    import openjarvis.server.tool_router as tr

    fake_pairs = [
        ("calendar_list_events", "calendar_list_events. List calendar meeting events for a user."),
        ("outlook_list_messages", "outlook_list_messages. List recent inbox email messages."),
        ("desktop_control", "desktop_control. Control desktop screen apps and files."),
        ("web_search", "web_search. Search the web for information."),
        ("memory_manage", "memory_manage. Manage persistent agent memory entries."),
    ]
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: fake_pairs)
    monkeypatch.setattr(tr, "_get_embedder", lambda: _FakeEmbedder())

    ranked = tr.rank_tools_for_query("calendar meeting", top_k=5)
    assert len(ranked) >= 1
    # The calendar tool should be at the top
    assert ranked[0]["name"] == "calendar_list_events"
    assert ranked[0]["score"] > 0.0


def test_top_k_cap_respected(router_env, monkeypatch):
    import openjarvis.server.tool_router as tr
    fake_pairs = [(f"tool_{i}", f"tool_{i}. token_{i} description.") for i in range(20)]
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: fake_pairs)
    monkeypatch.setattr(tr, "_get_embedder", lambda: _FakeEmbedder())
    ranked = tr.rank_tools_for_query("token_5 token_6", top_k=3)
    assert len(ranked) <= 3


def test_min_score_filters_noise(router_env, monkeypatch):
    """Bumping min_score should drop low-similarity tools."""
    import openjarvis.server.tool_router as tr
    fake_pairs = [
        ("a", "a. apple banana cherry."),
        ("b", "b. dog elephant fox."),
        ("c", "c. moon stars planet."),
    ]
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: fake_pairs)
    monkeypatch.setattr(tr, "_get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setenv("OPENJARVIS_TOOL_ROUTER_MIN_SCORE", "0.99")
    importlib.reload(tr)
    ranked = tr.rank_tools_for_query("apple banana", top_k=5)
    # With min_score=0.99 nothing matches well enough -> empty list
    assert ranked == []


def test_build_hint_block_format(router_env, monkeypatch):
    import openjarvis.server.tool_router as tr
    fake_pairs = [
        ("calendar_list_events", "calendar_list_events. List calendar events for the user."),
        ("send_email", "send_email. Compose and send an email message."),
    ]
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: fake_pairs)
    monkeypatch.setattr(tr, "_get_embedder", lambda: _FakeEmbedder())
    block = tr.build_tool_hint_block("calendar")
    assert block is not None
    assert "TOOL ROUTER" in block
    assert "calendar_list_events" in block
    assert "introspect_tools" in block  # tells the LLM about the meta-tool


def test_snapshot_fields(router_env, monkeypatch):
    import openjarvis.server.tool_router as tr
    snap = tr.snapshot()
    expected_keys = {
        "enabled", "top_k_default", "min_score", "embedder_ready",
        "embeddings_cached_count", "registry_hash", "cache_path",
        "cache_exists",
    }
    assert expected_keys <= set(snap.keys())


def test_cache_roundtrip(router_env, monkeypatch, tmp_path):
    """Embed once, then a fresh module load should pick up the cache
    without re-embedding."""
    import openjarvis.server.tool_router as tr
    fake_pairs = [
        ("a", "a. apple banana."),
        ("b", "b. cherry date."),
    ]
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: fake_pairs)
    monkeypatch.setattr(tr, "_get_embedder", lambda: _FakeEmbedder())
    # First call embeds and saves cache
    r1 = tr.rank_tools_for_query("apple")
    assert len(r1) >= 1
    assert tr._cache_path().exists()

    # Reset module state to force a fresh load from cache
    tr._TOOL_EMBEDDINGS = None
    tr._TOOL_NAMES = []
    tr._TOOL_DESCRIPTIONS = []
    tr._REGISTRY_HASH = None
    # _get_embedder still mocked to return _FakeEmbedder so dim matches
    r2 = tr.rank_tools_for_query("apple")
    assert len(r2) >= 1
    assert r2[0]["name"] == r1[0]["name"]


def test_registry_change_triggers_reembed(router_env, monkeypatch):
    """If the registry's tool set changes, the cache hash mismatches
    and the router re-embeds."""
    import openjarvis.server.tool_router as tr
    initial = [
        ("a", "a. apple."),
        ("b", "b. banana."),
    ]
    monkeypatch.setattr(tr, "_get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: initial)
    tr.rank_tools_for_query("apple")
    h1 = tr._REGISTRY_HASH

    # Add a new tool — hash should change on next call
    changed = initial + [("c", "c. cherry.")]
    monkeypatch.setattr(tr, "_enumerate_tools", lambda: changed)
    tr.rank_tools_for_query("cherry")
    h2 = tr._REGISTRY_HASH
    assert h1 != h2
    assert len(tr._TOOL_NAMES) == 3
