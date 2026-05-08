"""Introspect which integrations are configured.

Wraps the same logic the ``GET /v1/integrations/status`` route uses, so
the agent can introspect its own service env BEFORE asking the user to
set up environment variables that may already be set.

Why this tool exists
--------------------
Without it, the agent's failure mode for any integration question
(Outlook calendar, Stripe revenue, n8n workflow listing) is a generic
"please set OUTLOOK_CLIENT_ID, OUTLOOK_CLIENT_SECRET, ..." reply —
even when those variables are already configured. The agent has no way
to know what's actually present in ``os.environ`` unless we give it
one. Reading ``os.environ`` directly inside the OpenJarvis Python
process needs no Railway API call, no auth, no Cloudflare detour.

The tool is cheap and generic enough that the relevance filter in
``routes.py`` always includes it, so any integration question can be
answered with "I checked, here's what's wired up" rather than "please
configure X".
"""

from __future__ import annotations

import json
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("integrations_check")
class IntegrationsCheckTool(BaseTool):
    """Report which integrations have configured credentials.

    No external API calls — purely an ``os.environ`` introspection via
    the same helpers the ``/v1/integrations/status`` route uses. Returns
    a JSON map keyed by integration id (``outlook``, ``gmail``,
    ``calendar``, ``n8n``, ``stripe``, ``paypal``, ``cloudinary``,
    ``v0``, ``github``, ``railway``, ``obsidian``, etc.) with shape::

        {
          "outlook": {
            "configured": true,
            "healthy": true,
            "reason": "",
            "vars": [...]
          },
          ...
        }

    Always call this BEFORE telling the user to set up env vars. If the
    integration shows ``configured=true`` and ``healthy=true``, it's
    ready to use — pick the right tool from that integration's group
    and call it directly.
    """

    tool_id = "integrations_check"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="integrations_check",
            description=(
                "Check which integrations (gmail, outlook, calendar, "
                "n8n, stripe, paypal, github, railway, obsidian, etc.) "
                "have their credentials configured in this service's "
                "environment. ALWAYS call this first when the user asks "
                "about any integration, BEFORE telling them to set up "
                "env vars. Returns a per-integration map with "
                "{configured, healthy, reason, vars}. If "
                "configured=true the integration is ready — call its "
                "specific tool directly instead of asking the user to "
                "configure anything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "integration": {
                        "type": "string",
                        "description": (
                            "Optional: filter to a single integration "
                            "id (e.g. 'outlook'). If omitted, returns "
                            "the status of all known integrations."
                        ),
                    },
                },
                "required": [],
            },
            category="introspection",
            cost_estimate=0.0,
            latency_estimate=0.05,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            from openjarvis.server.integrations_routes import (
                _entries_by_integration,
                _probe,
            )
            from openjarvis.core.env import is_configured
        except Exception as exc:
            return ToolResult(
                tool_name="integrations_check",
                content=f"integrations_check unavailable: {exc}",
                success=False,
            )

        wanted = (params.get("integration") or "").strip().lower() or None

        grouped = _entries_by_integration()
        out: dict[str, Any] = {}
        for integration, specs in sorted(grouped.items()):
            if wanted and integration != wanted:
                continue
            var_states = []
            all_configured = True
            for spec in specs:
                ok = is_configured(spec.name)
                if not ok:
                    all_configured = False
                var_states.append(
                    {
                        "name": spec.name,
                        "configured": ok,
                        "purpose": spec.purpose,
                    }
                )
            healthy_probe, reason = _probe(integration)
            if healthy_probe is None:
                healthy = all_configured
                if not all_configured:
                    missing = [
                        v["name"] for v in var_states if not v["configured"]
                    ]
                    reason = f"missing env: {', '.join(missing)}"
            else:
                healthy = bool(healthy_probe and all_configured)
                if not all_configured:
                    missing = [
                        v["name"] for v in var_states if not v["configured"]
                    ]
                    reason = (reason + "; " if reason else "") + (
                        f"missing env: {', '.join(missing)}"
                    )
            out[integration] = {
                "configured": all_configured,
                "healthy": healthy,
                "reason": reason or "",
                "vars": var_states,
            }

        if wanted and not out:
            return ToolResult(
                tool_name="integrations_check",
                content=(
                    f"unknown integration {wanted!r}; known integrations: "
                    + ", ".join(sorted(grouped.keys()))
                ),
                success=False,
            )

        return ToolResult(
            tool_name="integrations_check",
            content=json.dumps({"integrations": out}, indent=2),
            success=True,
        )


__all__ = ["IntegrationsCheckTool"]
