"""Model-callable tools wrapping :class:`StripeClient`.

Read-only tools (revenue summary, list charges/subscriptions/refunds,
balance) carry no confirmation gate — they're inspections. The single
mutating tool, ``stripe_create_refund``, has ``requires_confirmation``
because it moves real money.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.stripe import (
    StripeClient,
    StripeUnavailableError,
    get_default_client,
)
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _ok(name: str, payload: Any) -> ToolResult:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = str(payload)
    return ToolResult(tool_name=name, content=payload or "(no content)", success=True)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"stripe error: {exc}", success=False)


class _StripeToolBase(BaseTool):
    is_local = False

    def __init__(self, client: Optional[StripeClient] = None) -> None:
        self._client = client or get_default_client()


@ToolRegistry.register("stripe_get_balance")
class StripeGetBalanceTool(_StripeToolBase):
    tool_id = "stripe_get_balance"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="stripe_get_balance",
            description="Current Stripe balance (available + pending) per currency.",
            parameters={"type": "object", "properties": {}},
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, self._client.get_balance())
        except StripeUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("stripe_revenue_summary")
class StripeRevenueSummaryTool(_StripeToolBase):
    tool_id = "stripe_revenue_summary"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="stripe_revenue_summary",
            description=(
                "Aggregate succeeded-charge revenue over the last N days. "
                "Returns gross, refunded, net per currency."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Look-back window in days (1-90).",
                    },
                },
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            days = int(params.get("days", 7))
            days = max(1, min(days, 90))
            return _ok(self.spec.name, self._client.revenue_summary(days=days))
        except StripeUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("stripe_list_charges")
class StripeListChargesTool(_StripeToolBase):
    tool_id = "stripe_list_charges"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="stripe_list_charges",
            description="List recent Stripe charges, optionally filtered by created_gte (Unix seconds).",
            parameters={
                "type": "object",
                "properties": {
                    "created_gte": {
                        "type": "integer",
                        "description": "Only charges created at or after this Unix timestamp (seconds).",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_charges(
                    created_gte=params.get("created_gte"),
                    limit=int(params.get("limit", 20)),
                ),
            )
        except StripeUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("stripe_list_subscriptions")
class StripeListSubscriptionsTool(_StripeToolBase):
    tool_id = "stripe_list_subscriptions"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="stripe_list_subscriptions",
            description="List Stripe subscriptions filtered by status (active/canceled/all).",
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "canceled", "all", "past_due", "trialing", "unpaid"],
                        "default": "active",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_subscriptions(
                    status=str(params.get("status", "active")),
                    limit=int(params.get("limit", 50)),
                ),
            )
        except StripeUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("stripe_list_refunds")
class StripeListRefundsTool(_StripeToolBase):
    tool_id = "stripe_list_refunds"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="stripe_list_refunds",
            description="List recent refunds, optionally filtered to a specific charge id.",
            parameters={
                "type": "object",
                "properties": {
                    "charge": {"type": "string", "description": "Charge id (ch_...)."},
                    "limit": {"type": "integer", "default": 20},
                },
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_refunds(
                    charge=params.get("charge"),
                    limit=int(params.get("limit", 20)),
                ),
            )
        except StripeUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("stripe_create_refund")
class StripeCreateRefundTool(_StripeToolBase):
    tool_id = "stripe_create_refund"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="stripe_create_refund",
            description=(
                "Refund a Stripe charge. amount in smallest currency unit "
                "(e.g. cents). Omit to refund the full amount."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "charge": {
                        "type": "string",
                        "description": "Charge id (ch_...) to refund.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Partial-refund amount in smallest currency unit.",
                    },
                    "reason": {
                        "type": "string",
                        "enum": ["duplicate", "fraudulent", "requested_by_customer"],
                    },
                },
                "required": ["charge"],
            },
            category="finance",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.create_refund(
                    charge=params["charge"],
                    amount=params.get("amount"),
                    reason=params.get("reason"),
                ),
            )
        except StripeUnavailableError as exc:
            return _err(self.spec.name, exc)


__all__ = [
    "StripeGetBalanceTool",
    "StripeRevenueSummaryTool",
    "StripeListChargesTool",
    "StripeListSubscriptionsTool",
    "StripeListRefundsTool",
    "StripeCreateRefundTool",
]
