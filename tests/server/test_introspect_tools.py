"""Round 20 Piece 2 — tests for the introspect_tools meta-tool.

Covers:
  * Tool is registered under "introspect_tools".
  * Empty query is rejected gracefully.
  * Disabled flag returns a failure result (not an exception).
  * When the router has results, the tool returns a JSON body with
    name + description + parameter schema for each match.
  * When the router returns nothing, the tool tells the LLM not to
    invent a tool name.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest


@pytest.fixture
def introspect_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("OPENJARVIS_INTROSPECT_TOOL_ENABLED", "true")
    # Reload both modules so they pick up the env
    import openjarvis.server.tool_router as tr
    importlib.reload(tr)
    import openjarvis.tools.introspect_tools as it
    importlib.reload(it)
    yield tmp_path


def test_tool_is_registered(introspect_env):
    from openjarvis.core.registry import ToolRegistry
    cls = ToolRegistry.get("introspect_tools")
    assert cls is not None
    inst = cls()
    spec = inst.spec
    assert spec.name == "introspect_tools"
    assert "query" in spec.parameters["properties"]
    assert "query" in spec.parameters["required"]


def test_empty_query_rejected(introspect_env):
    from openjarvis.core.registry import ToolRegistry
    cls = ToolRegistry.get("introspect_tools")
    result = cls().execute(query="")
    assert result.success is False
    assert "query" in result.content.lower()


def test_disabled_flag_returns_failure(introspect_env, monkeypatch):
    # _enabled() reads env at execute-time, no need to reload module.
    monkeypatch.setenv("OPENJARVIS_INTROSPECT_TOOL_ENABLED", "false")
    from openjarvis.core.registry import ToolRegistry
    cls = ToolRegistry.get("introspect_tools")
    result = cls().execute(query="check email")
    assert result.success is False
    assert "disabled" in result.content.lower()


def test_returns_ranked_results_with_schema(introspect_env, monkeypatch):
    """When the router has matches, each result includes the tool's
    parameter schema so the LLM can call it next."""
    import openjarvis.server.tool_router as tr

    # Use introspect_tools itself as the "found" tool — the autouse
    # registry-clear fixture leaves us with only what the
    # introspect_env fixture re-registered.
    def fake_rank(query, top_k=None):
        return [
            {"name": "introspect_tools", "description": "introspect_tools. Search the registry.", "score": 0.85},
        ]
    monkeypatch.setattr(tr, "rank_tools_for_query", fake_rank)

    from openjarvis.core.registry import ToolRegistry
    cls = ToolRegistry.get("introspect_tools")
    result = cls().execute(query="find tools")
    assert result.success is True
    body = json.loads(result.content)
    assert body["count"] == 1
    assert body["tools"][0]["name"] == "introspect_tools"
    # Parameter schema is enriched from the actual registered tool
    assert "parameters" in body["tools"][0]
    assert body["tools"][0]["parameters"]["type"] == "object"


def test_empty_results_warns_against_hallucination(introspect_env, monkeypatch):
    """When nothing matches, the tool body tells the LLM not to invent
    a tool name. This is the anti-`google_bridge` instruction."""
    import openjarvis.server.tool_router as tr
    monkeypatch.setattr(tr, "rank_tools_for_query", lambda q, top_k=None: [])

    from openjarvis.core.registry import ToolRegistry
    cls = ToolRegistry.get("introspect_tools")
    result = cls().execute(query="something nobody can do")
    assert result.success is True
    assert "invent" in result.content.lower()


def test_limit_param_caps_results(introspect_env, monkeypatch):
    import openjarvis.server.tool_router as tr
    calls: dict = {}

    def fake_rank(query, top_k=None):
        calls["top_k"] = top_k
        return [{"name": f"t{i}", "description": f"t{i}. desc", "score": 0.9 - i * 0.01}
                for i in range(15)]
    monkeypatch.setattr(tr, "rank_tools_for_query", fake_rank)

    from openjarvis.core.registry import ToolRegistry
    cls = ToolRegistry.get("introspect_tools")
    result = cls().execute(query="anything", limit=5)
    assert result.success is True
    assert calls["top_k"] == 5
