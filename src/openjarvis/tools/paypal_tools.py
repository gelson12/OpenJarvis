"""Model-callable tools wrapping :class:`PayPalClient`.

Read-only tools (revenue summary, list transactions, get subscription /
refund) are inspections. ``paypal_create_refund`` carries
``requires_confirmation`` because it moves real money.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.paypal import (
    PayPalClient,
    PayPalUnavailableError,
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
    return ToolResult(tool_name=name, content=f"paypal error: {exc}", success=False)


class _PayPalToolBase(BaseTool):
    is_local = False

    def __init__(self, client: Optional[PayPalClient] = None) -> None:
        self._client = client or get_default_client()


@ToolRegistry.register("paypal_revenue_summary")
class PayPalRevenueSummaryTool(_PayPalToolBase):
    tool_id = "paypal_revenue_summary"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paypal_revenue_summary",
            description=(
                "Aggregate completed-transaction revenue over the last "
                "N days, grouped by currency."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Look-back window in days (1-31).",
                    },
                },
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            days = int(params.get("days", 7))
            # PayPal's reporting API supports a 31-day max window.
            days = max(1, min(days, 31))
            return _ok(self.spec.name, self._client.revenue_summary(days=days))
        except PayPalUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("paypal_list_transactions")
class PayPalListTransactionsTool(_PayPalToolBase):
    tool_id = "paypal_list_transactions"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paypal_list_transactions",
            description=(
                "List PayPal transactions in an ISO8601 date range. "
                "Defaults to the last 7 days."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "RFC 3339 start, e.g. 2026-05-01T00:00:00Z.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "RFC 3339 end, e.g. 2026-05-07T23:59:59Z.",
                    },
                    "page_size": {"type": "integer", "default": 100},
                    "page": {"type": "integer", "default": 1},
                },
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_transactions(
                    start_date=params.get("start_date"),
                    end_date=params.get("end_date"),
                    page_size=int(params.get("page_size", 100)),
                    page=int(params.get("page", 1)),
                ),
            )
        except PayPalUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("paypal_get_subscription")
class PayPalGetSubscriptionTool(_PayPalToolBase):
    tool_id = "paypal_get_subscription"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paypal_get_subscription",
            description="Fetch a PayPal subscription by id (I-XXX...).",
            parameters={
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "string"},
                },
                "required": ["subscription_id"],
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.get_subscription(params["subscription_id"]),
            )
        except PayPalUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("paypal_get_refund")
class PayPalGetRefundTool(_PayPalToolBase):
    tool_id = "paypal_get_refund"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paypal_get_refund",
            description="Fetch a PayPal refund by id.",
            parameters={
                "type": "object",
                "properties": {
                    "refund_id": {"type": "string"},
                },
                "required": ["refund_id"],
            },
            category="finance",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.get_refund(params["refund_id"]),
            )
        except PayPalUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("paypal_create_refund")
class PayPalCreateRefundTool(_PayPalToolBase):
    tool_id = "paypal_create_refund"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="paypal_create_refund",
            description=(
                "Refund a PayPal capture. amount_value is a decimal "
                "string (e.g. '19.99'). Omit to refund the full capture."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "capture_id": {
                        "type": "string",
                        "description": "Capture id to refund.",
                    },
                    "amount_value": {
                        "type": "string",
                        "description": "Partial refund amount as decimal string.",
                    },
                    "currency": {
                        "type": "string",
                        "default": "USD",
                        "description": "ISO 4217 currency code.",
                    },
                    "note_to_payer": {"type": "string"},
                },
                "required": ["capture_id"],
            },
            category="finance",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.create_refund(
                    capture_id=params["capture_id"],
                    amount_value=params.get("amount_value"),
                    currency=str(params.get("currency", "USD")),
                    note_to_payer=params.get("note_to_payer"),
                ),
            )
        except PayPalUnavailableError as exc:
            return _err(self.spec.name, exc)


__all__ = [
    "PayPalRevenueSummaryTool",
    "PayPalListTransactionsTool",
    "PayPalGetSubscriptionTool",
    "PayPalGetRefundTool",
    "PayPalCreateRefundTool",
]
