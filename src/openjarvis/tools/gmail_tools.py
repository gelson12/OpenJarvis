"""Model-callable tools wrapping :class:`GmailClient`.

Read tools (profile / labels / list / get / get_thread) are inspections.
Mutating tools — modify_message (label changes), trash_message,
send_message — carry ``requires_confirmation=True`` because they touch
the user's real inbox or send mail on their behalf.

Send-message in particular is the most sensitive: even with confirmation,
the system prompt should explicitly tell the agent to read back the
recipient + subject + body before invoking the tool.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.gmail import (
    GmailClient,
    GmailUnavailableError,
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
    return ToolResult(tool_name=name, content=f"gmail error: {exc}", success=False)


class _GmailToolBase(BaseTool):
    is_local = False

    def __init__(self, client: Optional[GmailClient] = None) -> None:
        self._client = client or get_default_client()


@ToolRegistry.register("gmail_get_profile")
class GmailGetProfileTool(_GmailToolBase):
    tool_id = "gmail_get_profile"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_get_profile",
            description=(
                "Return the authorized user's Gmail profile — email "
                "address, total messages/threads. Cheap auth probe."
            ),
            parameters={"type": "object", "properties": {}},
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, self._client.get_profile())
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_list_labels")
class GmailListLabelsTool(_GmailToolBase):
    tool_id = "gmail_list_labels"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_list_labels",
            description=(
                "List all labels on the account (system labels like "
                "INBOX/UNREAD/SENT plus user-defined ones)."
            ),
            parameters={"type": "object", "properties": {}},
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, self._client.list_labels())
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_list_messages")
class GmailListMessagesTool(_GmailToolBase):
    tool_id = "gmail_list_messages"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_list_messages",
            description=(
                "List Gmail message ids matching a search query. Use "
                "Gmail search syntax in `q` — same as the search bar: "
                "`is:unread`, `from:foo@bar.com`, `newer_than:1d`, "
                "`subject:invoice`. Returns ids only — call "
                "gmail_get_message for content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": (
                            "Gmail search query (e.g. 'is:unread "
                            "newer_than:1d')."
                        ),
                    },
                    "label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter to messages with these label ids "
                            "(use gmail_list_labels to discover ids)."
                        ),
                    },
                    "max_results": {"type": "integer", "default": 25},
                    "include_spam_trash": {"type": "boolean", "default": False},
                },
            },
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_messages(
                    q=params.get("q"),
                    label_ids=params.get("label_ids"),
                    max_results=int(params.get("max_results", 25)),
                    include_spam_trash=bool(params.get("include_spam_trash", False)),
                ),
            )
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_get_message")
class GmailGetMessageTool(_GmailToolBase):
    tool_id = "gmail_get_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_get_message",
            description=(
                "Fetch a single Gmail message by id, including headers "
                "and body. format=full (default) | metadata | minimal | raw."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "format": {
                        "type": "string",
                        "enum": ["full", "metadata", "minimal", "raw"],
                        "default": "full",
                    },
                },
                "required": ["message_id"],
            },
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.get_message(
                    str(params["message_id"]),
                    format=str(params.get("format", "full")),
                ),
            )
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_get_thread")
class GmailGetThreadTool(_GmailToolBase):
    tool_id = "gmail_get_thread"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_get_thread",
            description="Fetch an entire Gmail thread (conversation) by id.",
            parameters={
                "type": "object",
                "properties": {"thread_id": {"type": "string"}},
                "required": ["thread_id"],
            },
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.get_thread(str(params["thread_id"])),
            )
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_modify_message")
class GmailModifyMessageTool(_GmailToolBase):
    tool_id = "gmail_modify_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_modify_message",
            description=(
                "Add / remove labels on a message. Common patterns: "
                "remove UNREAD to mark read; remove INBOX to archive; "
                "add STARRED to star; add a custom-label id to file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "add_label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "remove_label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["message_id"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.modify_message(
                    str(params["message_id"]),
                    add_label_ids=params.get("add_label_ids"),
                    remove_label_ids=params.get("remove_label_ids"),
                ),
            )
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_trash_message")
class GmailTrashMessageTool(_GmailToolBase):
    tool_id = "gmail_trash_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_trash_message",
            description=(
                "Move a message to trash. Recoverable for 30 days."
            ),
            parameters={
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.trash_message(str(params["message_id"])),
            )
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("gmail_send_message")
class GmailSendMessageTool(_GmailToolBase):
    tool_id = "gmail_send_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_send_message",
            description=(
                "Send an email via the user's Gmail account. ALWAYS "
                "read back the recipient + subject + body before "
                "calling this tool — sent mail is NOT recoverable."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                    "thread_id": {
                        "type": "string",
                        "description": "Reply within an existing thread.",
                    },
                    "in_reply_to": {
                        "type": "string",
                        "description": "Message-Id header to thread under.",
                    },
                    "html": {
                        "type": "boolean",
                        "default": False,
                        "description": "Send as HTML (default plain text).",
                    },
                },
                "required": ["to", "subject", "body"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.send_message(
                    to=str(params["to"]),
                    subject=str(params["subject"]),
                    body=str(params["body"]),
                    cc=params.get("cc"),
                    bcc=params.get("bcc"),
                    thread_id=params.get("thread_id"),
                    in_reply_to=params.get("in_reply_to"),
                    html=bool(params.get("html", False)),
                ),
            )
        except GmailUnavailableError as exc:
            return _err(self.spec.name, exc)


__all__ = [
    "GmailGetProfileTool",
    "GmailListLabelsTool",
    "GmailListMessagesTool",
    "GmailGetMessageTool",
    "GmailGetThreadTool",
    "GmailModifyMessageTool",
    "GmailTrashMessageTool",
    "GmailSendMessageTool",
]
