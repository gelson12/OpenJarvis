"""Tests for resolve_auto_model_for_agent — the fix for the agent 404."""

from __future__ import annotations

import pytest

from openjarvis.server.stream_bridge import resolve_auto_model_for_agent


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
