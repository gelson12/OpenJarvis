"""The Kernel — the single authority that resolves a turn deterministically.

``resolve(text)`` walks the registered data capabilities in priority order. The
first one whose ``detect`` claims the turn owns it: it executes and returns a
final :class:`Outcome` (OK / EMPTY / ERROR / NEEDS_INPUT). If none claim it,
the result is PASSTHROUGH and the caller hands the turn to the LLM — but with
``manifest()`` injected as ground truth, so the LLM still cannot disavow a
capability that exists.

This replaces the old "inject data + ABSOLUTE INSTRUCTIONS + anti-disavow
guard + hope" stack with one deterministic decision.
"""

from __future__ import annotations

import logging
from typing import Callable, List

from openjarvis.kernel import calendar_capability, email_capability
from openjarvis.kernel.contracts import CapabilitySpec, Outcome

logger = logging.getLogger("openjarvis.kernel")

# Ordered: most-specific data capabilities first. Each entry is a module with
# ``resolve(text) -> Outcome`` and ``spec() -> CapabilitySpec``.
_CAPABILITIES: List = [
    calendar_capability,
    email_capability,
]


def resolve(text: str) -> Outcome:
    """Resolve a user turn through the data capabilities. Never raises."""
    if not text or not text.strip():
        return Outcome.passthrough()
    for cap in _CAPABILITIES:
        try:
            outcome = cap.resolve(text)
        except Exception as exc:  # noqa: BLE001 — a broken capability must not break the turn
            logger.warning("kernel capability %s raised: %s", getattr(cap, "NAME", cap), exc)
            continue
        if outcome is not None and not outcome.is_passthrough:
            logger.info(
                "kernel.resolved capability=%s status=%s",
                outcome.capability, outcome.status.value,
            )
            return outcome
    return Outcome.passthrough()


def specs() -> List[CapabilitySpec]:
    out: List[CapabilitySpec] = []
    for cap in _CAPABILITIES:
        try:
            out.append(cap.spec())
        except Exception:  # noqa: BLE001
            continue
    return out


def manifest() -> str:
    """A compact, factual statement of what the assistant can actually do —
    injected into the LLM prompt as GROUND TRUTH so the model can never claim a
    capability is missing when it isn't.

    Only includes capabilities that are actually wired (available=True) plus a
    short honest note for any that are configured-but-off, so the LLM neither
    over-promises nor disavows.
    """
    lines = [
        "GROUND TRUTH — your real capabilities (do NOT claim you lack any of "
        "these; the server executes them deterministically and hands you the "
        "result):",
    ]
    for s in specs():
        mark = "✓" if s.available else "•"
        suffix = "" if s.available else " (not yet authorised — say so plainly if asked)"
        detail = f" {s.detail}" if s.detail else ""
        lines.append(f"  {mark} {s.name}: {s.summary}{suffix}{detail}")
    lines.append(
        "If a request matches one of the ✓ capabilities, the answer you are "
        "given is REAL — summarise it; never say 'I don't have a tool for that.'"
    )
    return "\n".join(lines)
