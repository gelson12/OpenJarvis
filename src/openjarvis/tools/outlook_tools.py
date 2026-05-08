"""Model-callable tools wrapping :class:`OutlookClient` (Microsoft Graph mail).

Read tools (profile / folders / list / get) are inspections.
Mutating tools — update_message (read/flag), move, delete, send, reply —
carry ``requires_confirmation=True``. Send / delete are the highest-risk
ones; the system prompt should tell the agent to read back recipient +
subject + body before invoking send.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.outlook import (
    OutlookClient,
    OutlookUnavailableError,
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
    return ToolResult(tool_name=name, content=f"outlook error: {exc}", success=False)


class _OutlookToolBase(BaseTool):
    is_local = False

    def __init__(self, client: Optional[OutlookClient] = None) -> None:
        self._client = client or get_default_client()


@ToolRegistry.register("outlook_get_profile")
class OutlookGetProfileTool(_OutlookToolBase):
    tool_id = "outlook_get_profile"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_get_profile",
            description="Return the authorized Outlook user — email, name, id. Cheap auth probe.",
            parameters={"type": "object", "properties": {}},
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, self._client.get_profile())
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_list_folders")
class OutlookListFoldersTool(_OutlookToolBase):
    tool_id = "outlook_list_folders"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_list_folders",
            description="List Outlook mail folders (Inbox, Sent, Drafts, custom).",
            parameters={"type": "object", "properties": {}},
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, self._client.list_folders())
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_list_messages")
class OutlookListMessagesTool(_OutlookToolBase):
    tool_id = "outlook_list_messages"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_list_messages",
            description=(
                "List Outlook messages. Use `search` for full-text "
                "queries (KQL: 'subject:invoice'), `filter` for OData "
                "expressions (\"isRead eq false and importance eq "
                "'high'\"), and `folder_id` to scope to one folder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": (
                            "Folder id from outlook_list_folders. "
                            "Special values: 'inbox', 'drafts', "
                            "'sentitems', 'deleteditems'."
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": "KQL full-text search.",
                    },
                    "filter": {
                        "type": "string",
                        "description": "OData $filter expression.",
                    },
                    "top": {"type": "integer", "default": 25},
                    "select": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Limit returned fields (subject, from, "
                            "receivedDateTime, isRead, ...). Reduces payload."
                        ),
                    },
                },
            },
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_messages(
                    folder_id=params.get("folder_id"),
                    search=params.get("search"),
                    filter_=params.get("filter"),
                    top=int(params.get("top", 25)),
                    select=params.get("select"),
                ),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_get_message")
class OutlookGetMessageTool(_OutlookToolBase):
    tool_id = "outlook_get_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_get_message",
            description="Fetch a single message by id (headers + body).",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "include_attachments": {"type": "boolean", "default": False},
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
                    include_attachments=bool(params.get("include_attachments", False)),
                ),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_update_message")
class OutlookUpdateMessageTool(_OutlookToolBase):
    tool_id = "outlook_update_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_update_message",
            description=(
                "Update a message — mark read/unread, flag/unflag. "
                "Cheaper than move; doesn't relocate."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "is_read": {"type": "boolean"},
                    "flag": {
                        "type": "string",
                        "enum": ["notFlagged", "flagged", "complete"],
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
                self._client.update_message(
                    str(params["message_id"]),
                    is_read=params.get("is_read"),
                    flag=params.get("flag"),
                ),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_move_message")
class OutlookMoveMessageTool(_OutlookToolBase):
    tool_id = "outlook_move_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_move_message",
            description=(
                "Move a message to another folder. Use destination_id "
                "from outlook_list_folders or special names 'inbox', "
                "'archive', 'deleteditems', 'junkemail'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "destination_id": {"type": "string"},
                },
                "required": ["message_id", "destination_id"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.move_message(
                    str(params["message_id"]),
                    destination_id=str(params["destination_id"]),
                ),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_delete_message")
class OutlookDeleteMessageTool(_OutlookToolBase):
    tool_id = "outlook_delete_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_delete_message",
            description=(
                "Permanently delete a message. Prefer "
                "outlook_move_message(destination_id='deleteditems') "
                "for recoverable soft-delete."
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
            self._client.delete_message(str(params["message_id"]))
            return _ok(self.spec.name, {"deleted": params["message_id"]})
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_send_message")
class OutlookSendMessageTool(_OutlookToolBase):
    tool_id = "outlook_send_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_send_message",
            description=(
                "Send an email via the user's Outlook account. ALWAYS "
                "read back recipient + subject + body before calling — "
                "sent mail is NOT recoverable. Comma-separate multiple "
                "addresses in to/cc/bcc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                    "html": {"type": "boolean", "default": False},
                    "save_to_sent_items": {"type": "boolean", "default": True},
                },
                "required": ["to", "subject", "body"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            self._client.send_message(
                to=str(params["to"]),
                subject=str(params["subject"]),
                body=str(params["body"]),
                cc=params.get("cc"),
                bcc=params.get("bcc"),
                html=bool(params.get("html", False)),
                save_to_sent_items=bool(params.get("save_to_sent_items", True)),
            )
            return _ok(self.spec.name, {"sent": True, "to": params["to"]})
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_reply_to_message")
class OutlookReplyToMessageTool(_OutlookToolBase):
    tool_id = "outlook_reply_to_message"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_reply_to_message",
            description=(
                "Reply to a message in-thread. Set reply_all=True to "
                "include all recipients of the original message."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "body": {"type": "string"},
                    "reply_all": {"type": "boolean", "default": False},
                    "html": {"type": "boolean", "default": False},
                },
                "required": ["message_id", "body"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            self._client.reply_to_message(
                str(params["message_id"]),
                body=str(params["body"]),
                reply_all=bool(params.get("reply_all", False)),
                html=bool(params.get("html", False)),
            )
            return _ok(self.spec.name, {"replied": params["message_id"]})
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


# ---------------------------------------------------------------------------
# Calendar (Outlook calendar via Microsoft Graph)
# ---------------------------------------------------------------------------


@ToolRegistry.register("outlook_list_events")
class OutlookListEventsTool(_OutlookToolBase):
    """List Outlook/Hotmail calendar events. Use start+end (ISO 8601) for
    time-windowed queries — e.g. start='2026-05-08T00:00:00Z' end=
    '2026-05-08T23:59:59Z' for 'today's events'. Without a window, lists
    upcoming events ordered by start time."""

    tool_id = "outlook_list_events"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_list_events",
            description=(
                "List events on the authorized user's Outlook / Hotmail "
                "calendar. For 'what's on my calendar today' style "
                "queries, pass start + end as ISO 8601 strings with "
                "timezone (e.g. start='2026-05-08T00:00:00Z' "
                "end='2026-05-08T23:59:59Z'). Without start/end, returns "
                "upcoming events ordered by start time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": (
                            "Window start (ISO 8601 with timezone). "
                            "Optional. If provided, also pass end."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": "Window end (ISO 8601). Optional.",
                    },
                    "top": {
                        "type": "integer",
                        "description": "Max events to return (default 25, max 100).",
                    },
                },
            },
            category="calendar",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.list_events(
                    start=params.get("start"),
                    end=params.get("end"),
                    top=int(params.get("top", 25)),
                ),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_get_event")
class OutlookGetEventTool(_OutlookToolBase):
    tool_id = "outlook_get_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_get_event",
            description=(
                "Fetch the full details of a single Outlook calendar "
                "event by id. Use after outlook_list_events to inspect a "
                "specific event."
            ),
            parameters={
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
            category="calendar",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.get_event(str(params["event_id"])),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_create_event")
class OutlookCreateEventTool(_OutlookToolBase):
    tool_id = "outlook_create_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_create_event",
            description=(
                "Create an event on the authorized user's Outlook calendar. "
                "Times in start/end are interpreted in the given timezone "
                "(default UTC). Read back subject + start + attendees "
                "before invoking — irreversible without a delete call."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 datetime, e.g. '2026-05-08T14:00:00'.",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 datetime.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone (default 'UTC').",
                    },
                    "body": {"type": "string", "description": "Event description."},
                    "location": {"type": "string"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of email addresses to invite.",
                    },
                    "is_all_day": {"type": "boolean"},
                },
                "required": ["subject", "start", "end"],
            },
            category="calendar",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.create_event(
                    subject=str(params["subject"]),
                    start=str(params["start"]),
                    end=str(params["end"]),
                    timezone=str(params.get("timezone", "UTC")),
                    body=params.get("body"),
                    location=params.get("location"),
                    attendees=params.get("attendees"),
                    is_all_day=bool(params.get("is_all_day", False)),
                ),
            )
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("outlook_delete_event")
class OutlookDeleteEventTool(_OutlookToolBase):
    tool_id = "outlook_delete_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="outlook_delete_event",
            description="Cancel/delete an event from the user's Outlook calendar.",
            parameters={
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
            category="calendar",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            self._client.delete_event(str(params["event_id"]))
            return _ok(self.spec.name, {"deleted": params["event_id"]})
        except OutlookUnavailableError as exc:
            return _err(self.spec.name, exc)


__all__ = [
    "OutlookGetProfileTool",
    "OutlookListFoldersTool",
    "OutlookListMessagesTool",
    "OutlookGetMessageTool",
    "OutlookUpdateMessageTool",
    "OutlookMoveMessageTool",
    "OutlookDeleteMessageTool",
    "OutlookSendMessageTool",
    "OutlookReplyToMessageTool",
    "OutlookListEventsTool",
    "OutlookGetEventTool",
    "OutlookCreateEventTool",
    "OutlookDeleteEventTool",
]
