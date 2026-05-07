"""Tests for resolve_auto_model_for_agent + FallbackEngine."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from openjarvis.server.stream_bridge import (
    FallbackEngine,
    get_agent_fallback_chain,
    resolve_auto_model_for_agent,
)


@pytest.fixture(autouse=True)
def _clear_provider_keys(monkeypatch):
    """Start every test with no API keys so each test sets only what it needs."""
    for k in (
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "TOOL_CAPABLE_AGENT_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)


def test_non_auto_passes_through(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_auto_model_for_agent("gpt-4o") == "gpt-4o"
    assert resolve_auto_model_for_agent("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_auto_with_anthropic_key_picks_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_auto_model_for_agent("auto") == "claude-sonnet-4-6"


def test_auto_skips_to_deepseek_when_no_anthropic(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_auto_model_for_agent("auto") == "deepseek-chat"


def test_auto_falls_to_openai_only_as_last_resort(monkeypatch):
    """OpenAI is last because users frequently arrive with dead billing."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_auto_model_for_agent("auto") == "gpt-4o"


def test_auto_recognises_openjarvis_auto_alias(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert resolve_auto_model_for_agent("openjarvis-auto") == "claude-sonnet-4-6"
    assert resolve_auto_model_for_agent("openjarvis/auto") == "claude-sonnet-4-6"
    assert resolve_auto_model_for_agent("AUTO") == "claude-sonnet-4-6"


def test_explicit_override_via_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("TOOL_CAPABLE_AGENT_MODEL", "deepseek-chat")
    # Override wins even though Anthropic key is also set.
    assert resolve_auto_model_for_agent("auto") == "deepseek-chat"


def test_no_keys_returns_literal_auto(monkeypatch):
    """No API keys at all → return 'auto' so the 404 surfaces (diagnostic)."""
    assert resolve_auto_model_for_agent("auto") == "auto"


def test_empty_or_none_passes_through():
    assert resolve_auto_model_for_agent("") == ""
    assert resolve_auto_model_for_agent("ollama/llama3.2") == "ollama/llama3.2"


# ---------------------------------------------------------------------------
# get_agent_fallback_chain — full ordered candidate list
# ---------------------------------------------------------------------------


def test_chain_for_concrete_model_is_single_element(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    # Concrete user choice — no fallback.
    assert get_agent_fallback_chain("gpt-4o") == ["gpt-4o"]


def test_chain_for_auto_returns_all_available(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    chain = get_agent_fallback_chain("auto")
    assert chain == ["claude-sonnet-4-6", "deepseek-chat", "gpt-4o"]


def test_chain_skips_providers_without_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    chain = get_agent_fallback_chain("auto")
    assert chain == ["deepseek-chat"]


def test_chain_empty_when_no_keys(monkeypatch):
    assert get_agent_fallback_chain("auto") == []


def test_chain_override_collapses_to_single(monkeypatch):
    """Operator override is a hard single-model pin (no implicit fallback)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("TOOL_CAPABLE_AGENT_MODEL", "claude-opus-4-7")
    assert get_agent_fallback_chain("auto") == ["claude-opus-4-7"]


# ---------------------------------------------------------------------------
# FallbackEngine — silent fallback on generate() failures
# ---------------------------------------------------------------------------


class _ScriptedEngine:
    """Engine stub: each model returns a result or raises per a script."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.calls: list[str] = []

    def generate(self, messages, *, model: str, **kwargs) -> dict:
        self.calls.append(model)
        outcome = self.script[model]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def health(self) -> bool:
        return True


def test_fallback_first_candidate_wins():
    inner = _ScriptedEngine({
        "claude-sonnet-4-6": {"content": "hi from claude"},
        "deepseek-chat": {"content": "should not run"},
    })
    fb = FallbackEngine(inner, ["claude-sonnet-4-6", "deepseek-chat"])
    out = fb.generate([], model="ignored-by-wrapper")
    assert out == {"content": "hi from claude"}
    assert inner.calls == ["claude-sonnet-4-6"]  # only one call


def test_fallback_skips_failing_candidate():
    """First candidate raises → next candidate is tried silently."""
    class _Quota(Exception):
        pass

    inner = _ScriptedEngine({
        "claude-sonnet-4-6": _Quota("Anthropic 529 overloaded"),
        "deepseek-chat": {"content": "hi from deepseek"},
    })
    fb = FallbackEngine(inner, ["claude-sonnet-4-6", "deepseek-chat"])
    out = fb.generate([], model="ignored")
    assert out == {"content": "hi from deepseek"}
    assert inner.calls == ["claude-sonnet-4-6", "deepseek-chat"]


def test_fallback_iterates_until_success():
    """Two failures, third succeeds — agent never sees the failures."""
    inner = _ScriptedEngine({
        "claude-sonnet-4-6": RuntimeError("anthropic 429"),
        "deepseek-chat": RuntimeError("deepseek 502"),
        "gemini-2.5-flash": {"content": "hi from gemini"},
    })
    fb = FallbackEngine(inner, ["claude-sonnet-4-6", "deepseek-chat", "gemini-2.5-flash"])
    out = fb.generate([], model="ignored")
    assert out == {"content": "hi from gemini"}
    assert inner.calls == ["claude-sonnet-4-6", "deepseek-chat", "gemini-2.5-flash"]


def test_fallback_raises_last_error_when_all_fail():
    """Every candidate fails → last exception is raised verbatim."""
    last_exc = RuntimeError("openai insufficient_quota")
    inner = _ScriptedEngine({
        "claude-sonnet-4-6": RuntimeError("anthropic boom"),
        "gpt-4o": last_exc,
    })
    fb = FallbackEngine(inner, ["claude-sonnet-4-6", "gpt-4o"])
    with pytest.raises(RuntimeError) as exc_info:
        fb.generate([], model="ignored")
    assert exc_info.value is last_exc


def test_fallback_delegates_other_methods():
    """stream/health/close/etc. pass through to the inner engine."""
    inner = _ScriptedEngine({"x": {"content": ""}})
    fb = FallbackEngine(inner, ["x"])
    # health() must reach the inner engine via __getattr__.
    assert fb.health() is True


def test_fallback_rejects_empty_candidates():
    inner = MagicMock()
    with pytest.raises(ValueError):
        FallbackEngine(inner, [])
