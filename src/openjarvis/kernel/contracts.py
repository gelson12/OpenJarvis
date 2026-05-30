"""OpenJarvis Kernel — the deterministic response contract.

WHY THIS EXISTS
---------------
Before the kernel, the server let the LLM decide two things it must never
decide:

  1. Whether a capability exists  ("I don't have a tool for Outlook" — said
     one turn after the server had *just* fetched the user's Outlook calendar).
  2. Whether an action succeeded   ("no houses to rent in Lisbon" — said when
     both booking providers had thrown DNS errors).

The tools themselves already know the truth: ``ToolResult.success`` is True for
real data and False for an error. The old ``intent_preexec`` discarded that
flag, injected ``.content`` regardless, and prayed the LLM would summarise it
instead of disavowing. It often didn't.

THE CONTRACT
------------
Every capability resolves to exactly one :class:`Outcome`. Its ``status`` is the
single source of truth for what happened:

    OK            — real data; speak ``message`` (built deterministically).
    EMPTY         — the action ran and there is genuinely nothing (zero events,
                    zero listings). Distinct from ERROR on purpose.
    ERROR         — the capability exists but could not complete (auth expired,
                    network down). We say so honestly; we NEVER pretend it was
                    EMPTY.
    NEEDS_INPUT   — the request is understood but a slot is missing (which city?).
    PASSTHROUGH   — no capability claims this turn; hand it to the LLM (which is
                    given the capability manifest as ground truth so it still
                    cannot disavow).

The LLM is never asked "can you do X?" — the registry answers that. This is the
foundation that makes the assistant reliable and predictable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class OutcomeStatus(str, enum.Enum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    NEEDS_INPUT = "needs_input"
    PASSTHROUGH = "passthrough"


@dataclass(slots=True)
class Outcome:
    """The result of resolving a turn through the kernel.

    ``message`` is the exact text to speak / return to the user. For every
    status except PASSTHROUGH it is final — the LLM is bypassed entirely.
    """

    status: OutcomeStatus
    message: str = ""
    # The capability that produced this outcome (for telemetry / tests).
    capability: Optional[str] = None
    # Structured payload (events, listings, …) — handy for UI widgets / tests.
    data: Dict[str, Any] = field(default_factory=dict)

    # ── Ergonomic constructors ──────────────────────────────────────────
    @classmethod
    def ok(cls, message: str, *, capability: str | None = None, **data: Any) -> "Outcome":
        return cls(OutcomeStatus.OK, message, capability, dict(data))

    @classmethod
    def empty(cls, message: str, *, capability: str | None = None, **data: Any) -> "Outcome":
        return cls(OutcomeStatus.EMPTY, message, capability, dict(data))

    @classmethod
    def error(cls, message: str, *, capability: str | None = None, **data: Any) -> "Outcome":
        return cls(OutcomeStatus.ERROR, message, capability, dict(data))

    @classmethod
    def needs_input(cls, message: str, *, capability: str | None = None, **data: Any) -> "Outcome":
        return cls(OutcomeStatus.NEEDS_INPUT, message, capability, dict(data))

    @classmethod
    def passthrough(cls) -> "Outcome":
        return cls(OutcomeStatus.PASSTHROUGH)

    # ── Predicates ──────────────────────────────────────────────────────
    @property
    def is_passthrough(self) -> bool:
        return self.status is OutcomeStatus.PASSTHROUGH

    @property
    def is_final(self) -> bool:
        """True when the kernel has produced the user-facing answer itself and
        the LLM must be skipped."""
        return self.status is not OutcomeStatus.PASSTHROUGH and bool(self.message)


@dataclass(slots=True)
class CapabilitySpec:
    """Declarative description of a capability — the ground truth the LLM is
    given so it can never claim a capability is missing.

    ``available`` reflects live configuration (e.g. is OUTLOOK wired?). When a
    capability is configured-but-degraded the registry still reports it as a
    real capability and lets the deterministic path surface the honest ERROR.
    """

    name: str
    summary: str               # one line: what the user can ask for
    available: bool = True
    detail: str = ""           # optional extra context for the manifest
