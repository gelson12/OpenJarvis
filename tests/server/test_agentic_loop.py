"""Round 20 Piece 3 — tests for the plan-act-verify agentic loop.

Covers the four loop branches:
  * tool_calls present -> execute via ToolRegistry, append result,
    re-enter LLM.
  * narration without action -> re-prompt the LLM to actually emit
    the tool_call.
  * clean final answer -> return immediately.
  * max iterations hit -> return what we have with halted_by signal.

Plus the validation layer:
  * Hallucinated tool name -> rejected with a clear reason BEFORE
    execution attempt. (The `google_bridge` failure mode.)
  * Tool execution exception -> structured failure, loop continues.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


# ---------- Test scaffolding ----------


class _FakeOK(BaseTool):
    """A registered tool that always succeeds."""

    tool_id = "_fake_ok"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="_fake_ok",
            description="Always-succeeding fake tool.",
            parameters={"type": "object", "properties": {}},
        )

    def execute(self, **params: Any) -> ToolResult:
        return ToolResult(
            tool_name="_fake_ok",
            success=True,
            content=json.dumps({"ok": True, "echo": params}),
        )


class _FakeFail(BaseTool):
    tool_id = "_fake_fail"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="_fake_fail",
            description="Always-failing fake tool.",
            parameters={"type": "object", "properties": {}},
        )

    def execute(self, **params: Any) -> ToolResult:
        return ToolResult(
            tool_name="_fake_fail",
            success=False,
            content="simulated failure",
        )


@pytest.fixture
def tools_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENJARVIS_AGENTIC_LOOP_ENABLED", "true")
    monkeypatch.setenv("OPENJARVIS_AGENTIC_LOOP_MAX_ITERATIONS", "3")
    # Register tools manually so we have a known set even after the
    # autouse registry-clear runs.
    ToolRegistry.register_value("_fake_ok", _FakeOK)
    ToolRegistry.register_value("_fake_fail", _FakeFail)
    yield


# ---------- Validation layer ----------


def test_validate_existing_tool(tools_env):
    from openjarvis.server.agentic_loop import validate_tool_call
    valid, reason = validate_tool_call("_fake_ok")
    assert valid is True
    assert reason == ""


def test_validate_hallucinated_tool(tools_env):
    """The `google_bridge` failure mode: validate rejects unknown names."""
    from openjarvis.server.agentic_loop import validate_tool_call
    valid, reason = validate_tool_call("google_bridge")
    assert valid is False
    assert "does not exist" in reason.lower()


def test_validate_empty_name():
    from openjarvis.server.agentic_loop import validate_tool_call
    valid, reason = validate_tool_call("")
    assert valid is False


# ---------- Tool execution ----------


def test_execute_real_tool(tools_env):
    from openjarvis.server.agentic_loop import execute_tool_call
    out = execute_tool_call("_fake_ok", {"x": 1})
    assert out.success is True
    assert out.name == "_fake_ok"
    body = json.loads(out.content)
    assert body["ok"] is True
    assert body["echo"]["x"] == 1


def test_execute_hallucinated_tool(tools_env):
    """Validation rejects before execution attempt."""
    from openjarvis.server.agentic_loop import execute_tool_call
    out = execute_tool_call("google_bridge", {})
    assert out.success is False
    assert out.validation_error is not None
    assert "does not exist" in out.validation_error.lower()


def test_execute_failing_tool(tools_env):
    from openjarvis.server.agentic_loop import execute_tool_call
    out = execute_tool_call("_fake_fail", {})
    assert out.success is False
    assert "failure" in out.content.lower()


# ---------- Tool-call extraction ----------


def test_extract_tool_calls_openai_format():
    from openjarvis.server.agentic_loop import extract_tool_calls
    response = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "_fake_ok",
                        "arguments": '{"x": 42}',
                    },
                }],
            },
        }],
    }
    calls = extract_tool_calls(response)
    assert len(calls) == 1
    assert calls[0]["name"] == "_fake_ok"
    assert calls[0]["arguments"]["x"] == 42


def test_extract_tool_calls_no_calls():
    from openjarvis.server.agentic_loop import extract_tool_calls
    response = {"choices": [{"message": {"content": "Hi", "tool_calls": None}}]}
    assert extract_tool_calls(response) == []


def test_extract_tool_calls_malformed_args():
    from openjarvis.server.agentic_loop import extract_tool_calls
    response = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {"name": "x", "arguments": "not valid json"},
                }],
            },
        }],
    }
    calls = extract_tool_calls(response)
    assert len(calls) == 1
    assert calls[0]["arguments"] == {}


# ---------- Narration detection ----------


def test_narration_detected_when_no_calls():
    from openjarvis.server.agentic_loop import looks_like_narration_without_action
    cases = [
        "I'll check your calendar for tomorrow.",
        "Let me look that up for you.",
        "One moment sir, let me check.",
        "Hold on while I search the web.",
    ]
    for text in cases:
        assert looks_like_narration_without_action(text, tool_calls=None) is True


def test_narration_not_flagged_when_tool_calls_present():
    from openjarvis.server.agentic_loop import looks_like_narration_without_action
    assert looks_like_narration_without_action(
        "I'll check your calendar",
        tool_calls=[{"id": "x"}],
    ) is False


def test_narration_not_flagged_for_clean_answer():
    from openjarvis.server.agentic_loop import looks_like_narration_without_action
    assert looks_like_narration_without_action(
        "You have no meetings tomorrow, sir.",
        tool_calls=None,
    ) is False


# ---------- Main loop branches ----------


def test_loop_disabled_raises(monkeypatch):
    monkeypatch.setenv("OPENJARVIS_AGENTIC_LOOP_ENABLED", "false")
    from openjarvis.server.agentic_loop import run_loop
    with pytest.raises(RuntimeError):
        run_loop(messages=[], llm_call=lambda m: {})


def test_loop_clean_final_answer(tools_env):
    """LLM returns a clean answer on first call — loop returns it."""
    from openjarvis.server.agentic_loop import run_loop

    def llm(_msgs):
        return {"choices": [{"message": {
            "content": "All done, sir.", "tool_calls": None,
        }}]}

    final, trace = run_loop(
        messages=[{"role": "user", "content": "hi"}],
        llm_call=llm,
    )
    assert final == "All done, sir."
    assert trace.iterations == 1
    assert trace.halted_by == "complete"
    assert len(trace.tool_calls) == 0


def test_loop_executes_tool_then_returns(tools_env):
    """Iter 1: LLM calls _fake_ok. Iter 2: LLM sees tool result and
    answers cleanly."""
    from openjarvis.server.agentic_loop import run_loop

    state = {"calls": 0}

    def llm(msgs):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "_fake_ok", "arguments": "{}"},
                }],
            }}]}
        return {"choices": [{"message": {
            "content": "Tool ran successfully.", "tool_calls": None,
        }}]}

    final, trace = run_loop(
        messages=[{"role": "user", "content": "do it"}],
        llm_call=llm,
    )
    assert final == "Tool ran successfully."
    assert trace.iterations == 2
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].success is True
    assert trace.halted_by == "complete"


def test_loop_rejects_hallucinated_tool(tools_env):
    """LLM emits a call to nonexistent `google_bridge` -> validation
    fails, tool message contains the rejection, loop continues."""
    from openjarvis.server.agentic_loop import run_loop

    state = {"calls": 0}

    def llm(msgs):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"choices": [{"message": {
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "google_bridge", "arguments": "{}"},
                }],
            }}]}
        return {"choices": [{"message": {
            "content": "Sorry, that tool doesn't exist.",
        }}]}

    final, trace = run_loop(
        messages=[{"role": "user", "content": "check google"}],
        llm_call=llm,
    )
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].success is False
    assert trace.tool_calls[0].validation_error is not None
    assert "google_bridge" in trace.tool_calls[0].validation_error


def test_loop_retries_on_narration(tools_env):
    """Iter 1: LLM narrates 'I'll check' without calling. Iter 2: it
    actually calls. Iter 3: it returns the answer."""
    from openjarvis.server.agentic_loop import run_loop

    state = {"calls": 0}

    def llm(msgs):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"choices": [{"message": {
                "content": "I'll check your data, give me a moment.",
                "tool_calls": None,
            }}]}
        if state["calls"] == 2:
            return {"choices": [{"message": {
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "_fake_ok", "arguments": "{}"},
                }],
            }}]}
        return {"choices": [{"message": {"content": "Here is the result."}}]}

    final, trace = run_loop(
        messages=[{"role": "user", "content": "do it"}],
        llm_call=llm,
        max_iter=5,
    )
    assert final == "Here is the result."
    assert trace.narration_retries == 1
    assert len(trace.tool_calls) == 1


def test_loop_max_iterations_halts(tools_env):
    """If the LLM keeps narrating forever, loop halts at max_iter."""
    from openjarvis.server.agentic_loop import run_loop

    def llm(_msgs):
        # Phrasing that DOES match the narration regex ("I'll check")
        return {"choices": [{"message": {
            "content": "I'll check on that, sir.", "tool_calls": None,
        }}]}

    final, trace = run_loop(
        messages=[{"role": "user", "content": "anything"}],
        llm_call=llm,
        max_iter=2,
    )
    assert trace.halted_by == "max_iterations"
    assert trace.narration_retries == 2


def test_trace_to_dict(tools_env):
    from openjarvis.server.agentic_loop import (
        AgenticLoopTrace, ToolCallOutcome,
    )
    t = AgenticLoopTrace(
        iterations=2,
        tool_calls=[ToolCallOutcome(
            name="x", arguments={}, success=True,
            content="r", latency_ms=42,
        )],
        final_response_text="ok",
        halted_by="complete",
    )
    d = t.to_dict()
    assert d["iterations"] == 2
    assert d["tool_calls"][0]["name"] == "x"
    assert d["final_response_preview"] == "ok"


def test_snapshot_fields(tools_env):
    from openjarvis.server.agentic_loop import snapshot
    s = snapshot()
    assert "enabled" in s
    assert "max_iterations" in s
    assert s["enabled"] is True
