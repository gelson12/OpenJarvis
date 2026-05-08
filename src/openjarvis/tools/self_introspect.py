"""Self-introspection tools — let the agent investigate its own service.

Exposes three tools that mirror what an operator with shell access
would do when debugging: list env vars, grep the source tree, read
specific source files. All scoped to OpenJarvis's own runtime so the
agent can answer "is X configured?" / "where is feature Y implemented?"
/ "what does function Z actually do?" without bothering the user.

Why these exist
---------------
The agent's failure mode for "what's on my Outlook calendar?" was a
generic Microsoft Azure setup wall — even though Outlook OAuth credentials
were 80% configured (only the refresh token was missing). The
``integrations_check`` tool addressed that one specific case for known
integrations. These three tools generalise: any time the agent suspects
its own state may explain a behaviour, it can introspect directly.

Security posture
----------------
- ``env_introspect`` returns variable NAMES freely; VALUES are redacted
  unless the variable name passes a known-non-secret allowlist
  (``URL``, ``EMAIL``, ``HOST``, ``PORT``, ``ENABLED``, etc. suffixes).
  The agent never needs the literal secret value to reason about
  configuration — "is OUTLOOK_REFRESH_TOKEN set?" is a yes/no question.
- ``source_grep`` and ``source_read`` are restricted to
  ``/app/src/openjarvis/`` plus the current working directory if it
  begins with ``/app``. They cannot escape into ``/etc``, ``/root``,
  user home dirs, etc. Path-traversal attempts (``..``) are rejected.
- All three tools refuse if invoked with empty / whitespace-only args.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


# ---------------------------------------------------------------------------
# env_introspect
# ---------------------------------------------------------------------------

# Variable-name suffixes / contains patterns that are SAFE to surface
# values for. Anything else gets the value redacted as "<set, len=N>"
# so the agent learns "configured: yes" without learning the secret.
_NON_SECRET_HINTS: tuple[str, ...] = (
    "URL", "URI", "ENDPOINT", "HOST", "PORT", "DOMAIN", "SCOPE",
    "ENABLED", "PATH", "MODE", "TYPE", "REGION", "VERSION",
    "EMAIL", "ACCOUNT", "USERNAME", "USER", "ID",
    "LIMIT", "TIMEOUT", "RETRIES",
)
# Hard-secret hints — even if matched by _NON_SECRET_HINTS above we
# still redact (e.g. "CLIENT_ID" matches ID but is sometimes treated
# as sensitive). When in doubt, redact.
_FORCE_REDACT_HINTS: tuple[str, ...] = (
    "TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "SIGNATURE", "PAT",
    "REFRESH", "ACCESS", "CREDENTIAL", "PRIVATE",
)


def _redact_value(name: str, value: str) -> str:
    """Decide whether to surface or redact this var's value."""
    upper = name.upper()
    for forced in _FORCE_REDACT_HINTS:
        if forced in upper:
            return f"<set, len={len(value)}>"
    for hint in _NON_SECRET_HINTS:
        if hint in upper:
            return value
    return f"<set, len={len(value)}>"


@ToolRegistry.register("env_introspect")
class EnvIntrospectTool(BaseTool):
    """List os.environ vars whose name matches a substring/pattern.

    Mirrors what ``railway variables --service OpenJarvis | grep X``
    does from outside the container, but reads ``os.environ`` directly
    inside the OpenJarvis Python process — no Railway API call (which
    Cloudflare blocks from inside Railway anyway), no auth, no extra
    round trip.

    Returns names always; values only for non-secret-looking vars
    (URLs, IDs, emails, hosts, ports). Sensitive vars (anything with
    TOKEN / SECRET / KEY / REFRESH / ACCESS / PASSWORD in the name)
    are reported as "<set, len=N>" so the agent learns "configured"
    without learning the literal secret.
    """

    tool_id = "env_introspect"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="env_introspect",
            description=(
                "Inspect this service's own env vars. Returns "
                "all variable names containing the given substring "
                "(case-insensitive). Use to confirm whether an "
                "integration is configured (e.g. pattern='OUTLOOK' "
                "lists OUTLOOK_Client_ID, OUTLOOK_Client_Secret, "
                "OUTLOOK_REFRESH_TOKEN, etc.). Values are redacted "
                "for sensitive names; safe values (URLs, IDs, emails) "
                "are returned in plaintext."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring to match against "
                            "env-var names. Examples: 'OUTLOOK', 'GOOGLE', "
                            "'STRIPE', 'TOKEN', 'OAUTH'."
                        ),
                    },
                },
                "required": ["pattern"],
            },
            category="introspection",
            cost_estimate=0.0,
            latency_estimate=0.01,
        )

    def execute(self, **params: Any) -> ToolResult:
        pattern = (params.get("pattern") or "").strip()
        if not pattern:
            return ToolResult(
                tool_name="env_introspect",
                content=(
                    "pattern is required (e.g. 'OUTLOOK'). To list ALL "
                    "vars, pass pattern='' explicitly is not allowed — "
                    "narrow the search."
                ),
                success=False,
            )
        needle = pattern.upper()
        matches: list[str] = []
        for name, value in sorted(os.environ.items()):
            if needle not in name.upper():
                continue
            shown = _redact_value(name, value)
            matches.append(f"{name} = {shown}")
        if not matches:
            return ToolResult(
                tool_name="env_introspect",
                content=(
                    f"No env vars matching {pattern!r}. The integration "
                    f"is NOT configured at this service. To use it, the "
                    f"required vars need to be set on this Railway service."
                ),
                success=True,
            )
        return ToolResult(
            tool_name="env_introspect",
            content="\n".join(matches),
            success=True,
        )


# ---------------------------------------------------------------------------
# source_grep — search OpenJarvis source tree
# ---------------------------------------------------------------------------

# Roots the agent may search/read. Anything outside is rejected.
_ALLOWED_ROOTS: tuple[Path, ...] = (
    Path("/app/src/openjarvis"),
    Path("/app/scripts"),
    Path("/app/frontend/src"),
    Path("/app/configs"),
)


def _is_allowed_path(p: Path) -> bool:
    """True if p is inside an allowed root (no path-traversal escapes)."""
    try:
        resolved = p.resolve()
    except Exception:
        return False
    for root in _ALLOWED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
        except Exception:
            continue
    return False


@ToolRegistry.register("source_grep")
class SourceGrepTool(BaseTool):
    """Grep OpenJarvis's own source tree for a pattern.

    Searches /app/src/openjarvis/, /app/scripts/, /app/frontend/src/,
    and /app/configs/ — the directories that contain THIS service's
    code and configuration. Cannot escape those roots.
    """

    tool_id = "source_grep"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="source_grep",
            description=(
                "Search this service's own source tree for a regex "
                "pattern. Use to find where a feature is implemented, "
                "what tools exist, what config options are read. "
                "Returns up to 50 matching lines with file:line "
                "prefixes. Examples: pattern='OUTLOOK_REFRESH_TOKEN' "
                "to find where the var is read; pattern='def stream_full' "
                "to find streaming entry points."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern (Python `re` syntax).",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional sub-path under the allowed roots "
                            "to narrow the search. Defaults to "
                            "/app/src/openjarvis. Must stay within the "
                            "allowed roots."
                        ),
                    },
                },
                "required": ["pattern"],
            },
            category="introspection",
            cost_estimate=0.0,
            latency_estimate=0.5,
        )

    def execute(self, **params: Any) -> ToolResult:
        pattern = (params.get("pattern") or "").strip()
        if not pattern:
            return ToolResult(
                tool_name="source_grep",
                content="pattern is required",
                success=False,
            )
        path_str = (params.get("path") or "/app/src/openjarvis").strip()
        path = Path(path_str)
        if not _is_allowed_path(path):
            return ToolResult(
                tool_name="source_grep",
                content=(
                    f"path {path_str!r} is outside allowed roots: "
                    + ", ".join(str(r) for r in _ALLOWED_ROOTS)
                ),
                success=False,
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            return ToolResult(
                tool_name="source_grep",
                content=f"invalid regex: {exc}",
                success=False,
            )
        # Use ripgrep if available; else fall back to walking + python re
        try:
            proc = subprocess.run(
                [
                    "rg", "--no-heading", "--line-number",
                    "--max-count", "5", "--max-columns", "200",
                    pattern, str(path),
                ],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 or proc.returncode == 1:
                lines = (proc.stdout or "").splitlines()[:50]
                if not lines:
                    return ToolResult(
                        tool_name="source_grep",
                        content=f"no matches for {pattern!r} in {path_str}",
                        success=True,
                    )
                return ToolResult(
                    tool_name="source_grep",
                    content="\n".join(lines),
                    success=True,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: pure-python walk
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = None
        if rx is None:
            return ToolResult(
                tool_name="source_grep",
                content="invalid regex",
                success=False,
            )
        matches: list[str] = []
        for root, _, files in os.walk(path):
            for f in files:
                if not f.endswith(
                    (".py", ".ts", ".tsx", ".js", ".jsx",
                     ".toml", ".yaml", ".yml", ".json", ".md")
                ):
                    continue
                fp = Path(root) / f
                try:
                    text = fp.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{fp}:{i}:{line.strip()[:200]}")
                        if len(matches) >= 50:
                            break
                if len(matches) >= 50:
                    break
            if len(matches) >= 50:
                break
        if not matches:
            return ToolResult(
                tool_name="source_grep",
                content=f"no matches for {pattern!r} in {path_str}",
                success=True,
            )
        return ToolResult(
            tool_name="source_grep",
            content="\n".join(matches),
            success=True,
        )


# ---------------------------------------------------------------------------
# source_read — read a specific source file
# ---------------------------------------------------------------------------


@ToolRegistry.register("source_read")
class SourceReadTool(BaseTool):
    """Read a file from this service's own source tree.

    Restricted to the same roots as source_grep. Reads up to 500 lines
    by default, optionally with offset for large files. Use after
    source_grep narrows down which file to inspect.
    """

    tool_id = "source_read"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="source_read",
            description=(
                "Read a file from this service's own source tree. "
                "Use AFTER source_grep narrows down a file. Restricted "
                "to /app/src/openjarvis/, /app/scripts/, /app/frontend/src/, "
                "and /app/configs/. Returns the file contents (up to 500 "
                "lines by default; pass offset/limit for large files)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path under an allowed root. "
                            "E.g. '/app/src/openjarvis/server/routes.py'."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (1-indexed). Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return. Default 500.",
                    },
                },
                "required": ["path"],
            },
            category="introspection",
            cost_estimate=0.0,
            latency_estimate=0.05,
        )

    def execute(self, **params: Any) -> ToolResult:
        path_str = (params.get("path") or "").strip()
        if not path_str:
            return ToolResult(
                tool_name="source_read",
                content="path is required",
                success=False,
            )
        path = Path(path_str)
        if not _is_allowed_path(path):
            return ToolResult(
                tool_name="source_read",
                content=(
                    f"path {path_str!r} is outside allowed roots: "
                    + ", ".join(str(r) for r in _ALLOWED_ROOTS)
                ),
                success=False,
            )
        if not path.exists():
            return ToolResult(
                tool_name="source_read",
                content=f"file does not exist: {path_str}",
                success=False,
            )
        offset = int(params.get("offset") or 1)
        limit = int(params.get("limit") or 500)
        offset = max(1, offset)
        limit = min(2000, max(1, limit))
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            return ToolResult(
                tool_name="source_read",
                content=f"read failed: {exc}",
                success=False,
            )
        lines = text.splitlines()
        chunk = lines[offset - 1: offset - 1 + limit]
        prefix = f"{path}\n--- lines {offset}-{offset + len(chunk) - 1} of {len(lines)} ---\n"
        body = "\n".join(f"{offset + i:5}\t{line}" for i, line in enumerate(chunk))
        return ToolResult(
            tool_name="source_read",
            content=prefix + body,
            success=True,
        )


__all__ = [
    "EnvIntrospectTool",
    "SourceGrepTool",
    "SourceReadTool",
]
