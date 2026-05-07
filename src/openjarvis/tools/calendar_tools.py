"""Model-callable tools wrapping :class:`GoogleCalendarClient`.

Read tools (list/get/freebusy/list_calendars) are inspections.
Mutating tools (create/update/delete) carry ``requires_confirmation``
because they affect the user's real schedule and may notify attendees.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.google_calendar import (
    GoogleCalendarClient,
    GoogleCalendarUnavailableError,
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
    return ToolResult(tool_name=name, content=f"calendar error: {exc}", success=False)


class _CalendarToolBase(BaseTool):
    is_local = False

    def __init__(self, client: Optional[GoogleCalendarClient] = None) -> None:
        self._client = client or get_default_client()


@ToolRegistry.register("calendar_list_calendars")
class CalendarListCalendarsTool(_CalendarToolBase):
    tool_id = "calendar_list_calendars"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_list_calendars",
            description="List Google calendars on the authorized account.",
            parameters={"type": "object", "properties": {}},
            category="calendar",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(self.spec.name, self._client.list_calendars())
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("calendar_list_events")
class CalendarListEventsTool(_CalendarToolBase):
    tool_id = "calendar_list_events"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_list_events",
            description=(
                "List Google Calendar events. Defaults to primary "
                "calendar from now forward. Use time_min/time_max "
                "(RFC3339) to bound, q to search."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string", "default": "primary"},
                    "time_min": {
                        "type": "string",
                        "description": "RFC3339 lower bound, e.g. 2026-05-08T00:00:00Z.",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "RFC3339 upper bound.",
                    },
                    "max_results": {"type": "integer", "default": 50},
                    "q": {
                        "type": "string",
                        "description": "Free-text search across event fields.",
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
                    calendar_id=str(params.get("calendar_id", "primary")),
                    time_min=params.get("time_min"),
                    time_max=params.get("time_max"),
                    max_results=int(params.get("max_results", 50)),
                    q=params.get("q"),
                ),
            )
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("calendar_get_event")
class CalendarGetEventTool(_CalendarToolBase):
    tool_id = "calendar_get_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_get_event",
            description="Fetch a single calendar event by id.",
            parameters={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "calendar_id": {"type": "string", "default": "primary"},
                },
                "required": ["event_id"],
            },
            category="calendar",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.get_event(
                    params["event_id"],
                    calendar_id=str(params.get("calendar_id", "primary")),
                ),
            )
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("calendar_freebusy")
class CalendarFreeBusyTool(_CalendarToolBase):
    tool_id = "calendar_freebusy"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_freebusy",
            description=(
                "Check free/busy across one or more calendars in a "
                "time range — answer 'when am I free?' style questions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "time_min": {
                        "type": "string",
                        "description": "RFC3339 start, e.g. 2026-05-08T00:00:00Z.",
                    },
                    "time_max": {
                        "type": "string",
                        "description": "RFC3339 end.",
                    },
                    "calendar_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Defaults to ['primary'] if omitted.",
                    },
                },
                "required": ["time_min", "time_max"],
            },
            category="calendar",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.freebusy_query(
                    time_min=params["time_min"],
                    time_max=params["time_max"],
                    calendar_ids=params.get("calendar_ids"),
                ),
            )
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("calendar_create_event")
class CalendarCreateEventTool(_CalendarToolBase):
    tool_id = "calendar_create_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_create_event",
            description=(
                "Create a calendar event. start/end shape: "
                "{'dateTime':'2026-05-08T10:00:00-04:00','timeZone':'America/New_York'} "
                "for timed events, or {'date':'2026-05-08'} for all-day."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "start": {
                        "type": "object",
                        "description": (
                            "Start time. {dateTime, timeZone} for timed, "
                            "{date} for all-day."
                        ),
                    },
                    "end": {
                        "type": "object",
                        "description": "End time, same shape as start.",
                    },
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "attendees": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"email": {"type": "string"}},
                        },
                        "description": "Optional list of {email: ...} dicts.",
                    },
                    "calendar_id": {"type": "string", "default": "primary"},
                    "send_updates": {
                        "type": "string",
                        "enum": ["none", "all", "externalOnly"],
                        "default": "none",
                    },
                },
                "required": ["summary", "start", "end"],
            },
            category="calendar",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.create_event(
                    summary=str(params["summary"]),
                    start=params["start"],
                    end=params["end"],
                    calendar_id=str(params.get("calendar_id", "primary")),
                    description=params.get("description"),
                    location=params.get("location"),
                    attendees=params.get("attendees"),
                    send_updates=str(params.get("send_updates", "none")),
                ),
            )
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("calendar_update_event")
class CalendarUpdateEventTool(_CalendarToolBase):
    tool_id = "calendar_update_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_update_event",
            description=(
                "PATCH an existing event. Only the fields in 'patch' are "
                "modified. Pass {'summary': '...'} to rename, "
                "{'start': {...}, 'end': {...}} to reschedule, etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "patch": {
                        "type": "object",
                        "description": "Partial event resource — fields to change.",
                    },
                    "calendar_id": {"type": "string", "default": "primary"},
                    "send_updates": {
                        "type": "string",
                        "enum": ["none", "all", "externalOnly"],
                        "default": "none",
                    },
                },
                "required": ["event_id", "patch"],
            },
            category="calendar",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return _ok(
                self.spec.name,
                self._client.update_event(
                    str(params["event_id"]),
                    params["patch"],
                    calendar_id=str(params.get("calendar_id", "primary")),
                    send_updates=str(params.get("send_updates", "none")),
                ),
            )
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


@ToolRegistry.register("calendar_delete_event")
class CalendarDeleteEventTool(_CalendarToolBase):
    tool_id = "calendar_delete_event"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_delete_event",
            description="Delete a calendar event. Cannot be undone.",
            parameters={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "calendar_id": {"type": "string", "default": "primary"},
                    "send_updates": {
                        "type": "string",
                        "enum": ["none", "all", "externalOnly"],
                        "default": "none",
                    },
                },
                "required": ["event_id"],
            },
            category="calendar",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            self._client.delete_event(
                str(params["event_id"]),
                calendar_id=str(params.get("calendar_id", "primary")),
                send_updates=str(params.get("send_updates", "none")),
            )
            return _ok(self.spec.name, {"deleted": params["event_id"]})
        except GoogleCalendarUnavailableError as exc:
            return _err(self.spec.name, exc)


__all__ = [
    "CalendarListCalendarsTool",
    "CalendarListEventsTool",
    "CalendarGetEventTool",
    "CalendarFreeBusyTool",
    "CalendarCreateEventTool",
    "CalendarUpdateEventTool",
    "CalendarDeleteEventTool",
]
