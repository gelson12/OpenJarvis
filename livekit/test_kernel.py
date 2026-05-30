"""Tests for the worker-side WorkerKernel and its device capabilities.

Run: PYTHONPATH=livekit python -m pytest livekit/test_kernel.py

Locks in the production fixes:
  * "open gesture mode" / "show gesture mode" turn it ON (used to fall through
    to the LLM, which said "I'm not sure what gesture mode is");
  * "how do I use gesture mode" explains instead of toggling or disavowing;
  * a publish failure is an honest ERROR, never silent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import kernel as K
from kernel import Outcome, OutcomeStatus, WorkerKernel


# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeLocalParticipant:
    def __init__(self, fail=False):
        self.published = []
        self._fail = fail

    async def publish_data(self, payload, reliable=True, topic=None):
        if self._fail:
            raise RuntimeError("data channel down")
        self.published.append((payload, topic))


class _FakeRoom:
    def __init__(self, fail=False):
        self.local_participant = _FakeLocalParticipant(fail)


class _FakeAgent:
    def __init__(self, fail=False):
        self._room = _FakeRoom(fail)


# ── Intent detection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("open gesture mode", True),
    ("show gesture mode", True),
    ("turn on gesture mode", True),
    ("activate gesture controls", True),
    ("launch gesture mode please", True),
    ("close gesture mode", False),
    ("turn off gesture mode", False),
    ("exit gesture mode", False),
    ("what's the weather", None),
])
def test_gesture_intent(text, expected):
    assert K.gesture_intent(text) is expected


def test_gesture_help_detection():
    assert K.gesture_is_help("how do I use gesture mode")
    assert K.gesture_is_help("what is gesture mode")
    assert K.gesture_is_help("give me instructions for gesture mode")
    assert not K.gesture_is_help("open gesture mode")
    assert not K.gesture_is_help("how is the weather")  # no gesture word


@pytest.mark.parametrize("text,expected", [
    ("turn on the camera", True),
    ("open the camera", True),
    ("show me the webcam", True),
    ("turn off the camera", False),
    ("hide the video", False),
    ("play some music", None),
])
def test_camera_intent(text, expected):
    assert K.camera_intent(text) is expected


# ── Capability execution ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gesture_open_turns_on_and_speaks():
    agent = _FakeAgent()
    out = await K.gesture_capability("open gesture mode", agent)
    assert out.status is OutcomeStatus.OK
    assert out.message == "Gesture mode on, sir."
    # camera-on + gesture_mode-on both published
    topics = [t for _, t in agent._room.local_participant.published]
    assert topics == ["ui-command", "ui-command"]


@pytest.mark.asyncio
async def test_gesture_help_explains_not_toggles():
    agent = _FakeAgent()
    out = await K.gesture_capability("how do I use gesture mode", agent)
    assert out.status is OutcomeStatus.OK
    assert "hands" in out.message.lower()
    # Nothing published — it explained, it didn't toggle.
    assert agent._room.local_participant.published == []


@pytest.mark.asyncio
async def test_gesture_publish_failure_is_honest_error():
    agent = _FakeAgent(fail=True)
    out = await K.gesture_capability("open gesture mode", agent)
    assert out.status is OutcomeStatus.ERROR
    assert "couldn't" in out.message.lower()


@pytest.mark.asyncio
async def test_camera_capability_passthrough_when_not_camera():
    out = await K.camera_capability("tell me a joke", _FakeAgent())
    assert out.is_passthrough


# ── Registry ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registry_first_match_wins_and_order():
    k = WorkerKernel()
    k.register("camera", K.camera_capability)
    k.register("gesture", K.gesture_capability)
    out = await k.resolve("open gesture mode", _FakeAgent())
    assert out.capability == "gesture" and out.status is OutcomeStatus.OK
    assert (await k.resolve("hello there", _FakeAgent())).is_passthrough


@pytest.mark.asyncio
async def test_legacy_adapter_handled_and_passthrough():
    k = WorkerKernel()

    async def _legacy_handled(text):
        return True

    async def _legacy_skip(text):
        return False

    k.register_legacy("handled_one", _legacy_handled)
    out = await k.resolve("anything", _FakeAgent())
    assert out.status is OutcomeStatus.HANDLED and out.speak == ""

    k2 = WorkerKernel()
    k2.register_legacy("skip_one", _legacy_skip)
    assert (await k2.resolve("anything", _FakeAgent())).is_passthrough


@pytest.mark.asyncio
async def test_broken_capability_does_not_break_turn():
    k = WorkerKernel()

    async def _boom(text, agent):
        raise ValueError("kaboom")

    k.register("boom", _boom)
    k.register("gesture", K.gesture_capability)
    # The broken capability is skipped; gesture still resolves.
    out = await k.resolve("open gesture mode", _FakeAgent())
    assert out.capability == "gesture"


def test_outcome_speak_semantics():
    assert Outcome.ok("hi").speak == "hi"
    assert Outcome.handled().speak == ""
    assert Outcome.passthrough().speak == ""
    assert Outcome.error("boom").stops_turn
