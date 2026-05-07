"""Tests for the always-surface change in elaboration_worker.

The original heuristic (_is_materially_different) marked elaborations as
DISCARDED whenever the Claude-CLI answer was similar in length and
content to the spoken answer — which made the parallel Claude track
invisible to users. Now elaborations are always surfaced by default
(ELABORATION_ALWAYS_SURFACE=true).
"""

from __future__ import annotations

import pytest

from openjarvis.server import elaboration_worker


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("ELABORATION_ALWAYS_SURFACE", raising=False)


def test_always_surface_default_true(monkeypatch):
    """Default behaviour is to surface every elaboration."""
    assert elaboration_worker._always_surface() is True


def test_always_surface_disabled_via_env(monkeypatch):
    """Operators can opt back into the diff heuristic."""
    monkeypatch.setenv("ELABORATION_ALWAYS_SURFACE", "false")
    assert elaboration_worker._always_surface() is False


def test_always_surface_accepts_truthy_aliases(monkeypatch):
    for v in ("true", "1", "yes", "on", "TRUE", "True"):
        monkeypatch.setenv("ELABORATION_ALWAYS_SURFACE", v)
        assert elaboration_worker._always_surface() is True
    for v in ("false", "0", "no", "off", "FALSE"):
        monkeypatch.setenv("ELABORATION_ALWAYS_SURFACE", v)
        assert elaboration_worker._always_surface() is False


def test_diff_heuristic_still_works_when_disabled(monkeypatch):
    """When always-surface is off, the diff heuristic still functions."""
    monkeypatch.setenv("ELABORATION_ALWAYS_SURFACE", "false")
    # Identical short answers — heuristic returns False (would discard).
    assert (
        elaboration_worker._is_materially_different("hello", "hello")
        is False
    )
    # Long, dissimilar answer — heuristic returns True (would surface).
    spoken = "It's 5pm."
    claude = (
        "It is currently 5:00 PM in your local timezone. "
        "Would you like me to convert this to other timezones, "
        "or look up calendar conflicts for the next hour?"
    )
    assert elaboration_worker._is_materially_different(spoken, claude) is True
