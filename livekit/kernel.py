"""Worker-side Kernel — the body's half of the deterministic capability layer.

The server kernel (``openjarvis.kernel``) owns DATA capabilities (calendar,
email). This owns DEVICE capabilities that are inherently client-side: the
camera, gesture mode, widgets, desktop control, accommodation, …

It enforces the SAME contract as the server kernel so the assistant behaves
one consistent way everywhere:

    Outcome ∈ { OK | EMPTY | ERROR | NEEDS_INPUT | HANDLED | PASSTHROUGH }

A capability that claims a turn is executed deterministically and returns a
final Outcome; the worker speaks ``message`` (or, for HANDLED, the capability
already spoke) and stops the turn. Nothing reaches the LLM unless every
capability passes (PASSTHROUGH) — and the LLM is then anchored by the server's
ground-truth manifest, so it still can't disavow.

Migration note: the legacy ``_maybe_handle_*`` methods on ``Assistant`` are
registered here via :func:`legacy` adapters, giving a single ordered registry
today. Each can be rewritten as a native capability over time WITHOUT another
architectural change — that is the whole point of the contract.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger("openjarvis.worker.kernel")


class OutcomeStatus(str, enum.Enum):
    OK = "ok"               # produced an answer; kernel speaks `message`
    EMPTY = "empty"         # ran, nothing to report; kernel speaks `message`
    ERROR = "error"         # capability exists but failed; honest `message`
    NEEDS_INPUT = "needs_input"
    HANDLED = "handled"     # capability already spoke / acted; just stop
    PASSTHROUGH = "passthrough"


@dataclass(slots=True)
class Outcome:
    status: OutcomeStatus
    message: str = ""
    capability: Optional[str] = None
    data: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, message: str, *, capability=None, **data) -> "Outcome":
        return cls(OutcomeStatus.OK, message, capability, dict(data))

    @classmethod
    def empty(cls, message: str, *, capability=None, **data) -> "Outcome":
        return cls(OutcomeStatus.EMPTY, message, capability, dict(data))

    @classmethod
    def error(cls, message: str, *, capability=None, **data) -> "Outcome":
        return cls(OutcomeStatus.ERROR, message, capability, dict(data))

    @classmethod
    def needs_input(cls, message: str, *, capability=None, **data) -> "Outcome":
        return cls(OutcomeStatus.NEEDS_INPUT, message, capability, dict(data))

    @classmethod
    def handled(cls, *, capability=None, **data) -> "Outcome":
        """The capability already spoke/acted; the kernel just stops the turn."""
        return cls(OutcomeStatus.HANDLED, "", capability, dict(data))

    @classmethod
    def passthrough(cls) -> "Outcome":
        return cls(OutcomeStatus.PASSTHROUGH)

    @property
    def is_passthrough(self) -> bool:
        return self.status is OutcomeStatus.PASSTHROUGH

    @property
    def stops_turn(self) -> bool:
        return self.status is not OutcomeStatus.PASSTHROUGH

    @property
    def speak(self) -> str:
        """Text the kernel should say (empty for HANDLED / PASSTHROUGH)."""
        if self.status in (OutcomeStatus.HANDLED, OutcomeStatus.PASSTHROUGH):
            return ""
        return self.message


# A capability is an async callable: (text, agent) -> Outcome
Capability = Callable[[str, Any], Awaitable[Outcome]]


@dataclass
class _Registered:
    name: str
    fn: Capability


class WorkerKernel:
    """Ordered registry. First capability to claim the turn wins."""

    def __init__(self) -> None:
        self._caps: List[_Registered] = []

    def register(self, name: str, fn: Capability) -> None:
        self._caps.append(_Registered(name, fn))

    def register_legacy(self, name: str, method: Callable[[str], Awaitable[bool]]) -> None:
        """Adapt a legacy ``_maybe_handle_*`` (returns True when handled and it
        has already spoken) into a capability."""
        async def _adapter(text: str, agent: Any, _m=method) -> Outcome:
            handled = await _m(text)
            return Outcome.handled(capability=name) if handled else Outcome.passthrough()
        self.register(name, _adapter)

    async def resolve(self, text: str, agent: Any) -> Outcome:
        """Walk capabilities in order. Never raises — a broken capability is
        skipped so the turn survives."""
        for cap in self._caps:
            try:
                outcome = await cap.fn(text, agent)
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker capability %s raised: %s", cap.name, exc)
                continue
            if outcome is not None and not outcome.is_passthrough:
                logger.info("worker.kernel resolved capability=%s status=%s",
                            cap.name, outcome.status.value)
                return outcome
        return Outcome.passthrough()


# ═══════════════════════════════════════════════════════════════════════════
# Device capabilities (native — built on the contract, not adapters)
# ═══════════════════════════════════════════════════════════════════════════

UI_COMMAND_TOPIC = "ui-command"

# ── Camera ─────────────────────────────────────────────────────────────────
_CAM_WORD = re.compile(r"\b(camera|cam|webcam|video)\b", re.I)
_CAM_OFF = re.compile(r"\b(off|disable|stop|close|kill|hide|turn it off|shut)\b", re.I)
_CAM_ON = re.compile(r"\b(on|enable|start|open|show|turn it on|activate|bring up)\b", re.I)


def camera_intent(text: str) -> Optional[bool]:
    """True=on, False=off, None=not a camera command."""
    if not text or not _CAM_WORD.search(text):
        return None
    if _CAM_OFF.search(text):
        return False
    if _CAM_ON.search(text):
        return True
    return None


# ── Gesture mode ─────────────────────────────────────────────────────────
_GESTURE_WORD = re.compile(r"\bgesture(?:\s+(?:mode|control|controls))?\b", re.I)
# "open" and "show" were MISSING before — "open gesture mode" fell through to
# the LLM, which said "I'm not sure what gesture mode is". Fixed here.
_GESTURE_ON = re.compile(
    r"\b(on|enable|start|activate|enter|begin|launch|open|show|bring up|turn it on)\b", re.I
)
_GESTURE_OFF = re.compile(
    r"\b(off|disable|stop|exit|leave|cancel|hide|close|end|turn it off)\b", re.I
)
# "how do I use gesture mode", "what is gesture mode", "instructions for gesture"
_GESTURE_HELP = re.compile(
    r"\b(how\s+(?:do|can|to)|what\s+is|what['’]?s|explain|instructions?|guide|"
    r"tutorial|teach\s+me|tell\s+me\s+about|how\s+does)\b", re.I
)

_GESTURE_HELP_TEXT = (
    "Gesture mode turns on your camera and overlays it across the dashboard, "
    "sir, so you can drive the HUD with your hands — point to highlight a "
    "panel, pinch to select, and move your hand to drag it. Just say "
    "'open gesture mode' to start and 'close gesture mode' to stop."
)


def gesture_intent(text: str) -> Optional[bool]:
    """True=on, False=off, None=not a toggle. 'off' wins on conflict."""
    if not text or not _GESTURE_WORD.search(text):
        return None
    if _GESTURE_OFF.search(text):
        return False
    if _GESTURE_ON.search(text):
        return True
    return None


def gesture_is_help(text: str) -> bool:
    return bool(text and _GESTURE_WORD.search(text) and _GESTURE_HELP.search(text))


async def _publish(agent: Any, payload: dict) -> None:
    import json
    await agent._room.local_participant.publish_data(
        json.dumps(payload).encode(), reliable=True, topic=UI_COMMAND_TOPIC,
    )


async def camera_capability(text: str, agent: Any) -> Outcome:
    want = camera_intent(text)
    if want is None:
        return Outcome.passthrough()
    try:
        await _publish(agent, {"type": "camera", "enabled": want})
    except Exception as exc:  # noqa: BLE001
        logger.error("camera publish failed: %s", exc)
        return Outcome.error("I couldn't reach the camera control just now, sir.",
                             capability="camera")
    return Outcome.ok("Camera on, sir." if want else "Camera off, sir.",
                      capability="camera", enabled=want)


async def gesture_capability(text: str, agent: Any) -> Outcome:
    # Help request takes priority over a toggle so "how do I use gesture mode"
    # explains instead of silently turning it on (or, as before, disavowing).
    if gesture_is_help(text) and gesture_intent(text) is None:
        return Outcome.ok(_GESTURE_HELP_TEXT, capability="gesture")
    want = gesture_intent(text)
    if want is None:
        return Outcome.passthrough()
    try:
        if want:
            # Force the camera on first so MediaPipe has frames to track.
            await _publish(agent, {"type": "camera", "enabled": True})
        await _publish(agent, {"type": "gesture_mode", "enabled": want})
    except Exception as exc:  # noqa: BLE001
        logger.error("gesture publish failed: %s", exc)
        return Outcome.error("I couldn't switch gesture mode just now, sir.",
                             capability="gesture")
    return Outcome.ok("Gesture mode on, sir." if want else "Gesture mode off, sir.",
                      capability="gesture", enabled=want)
