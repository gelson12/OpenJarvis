"""OpenJarvis Kernel — the deterministic capability layer.

The single authority for "can the assistant do X, and did it work". Capabilities
resolve a turn to an :class:`Outcome` (OK / EMPTY / ERROR / NEEDS_INPUT /
PASSTHROUGH) built from real tool results — the LLM is never asked whether a
capability exists, so it can never disavow one that does.

See ``docs/KERNEL.md`` for the contract every capability follows.
"""

from openjarvis.kernel.contracts import (
    CapabilitySpec,
    Outcome,
    OutcomeStatus,
)
from openjarvis.kernel.core import manifest, resolve, specs

__all__ = [
    "Outcome",
    "OutcomeStatus",
    "CapabilitySpec",
    "resolve",
    "specs",
    "manifest",
]
