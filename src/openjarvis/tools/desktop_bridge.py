"""Desktop control tool — operate the user's Windows machines via LiveKit.

A ``desktop-bridge`` process runs on each Windows machine (laptop, ROG),
connects OUTBOUND to LiveKit Cloud, and joins the control room. This tool —
running inside the OpenJarvis backend — joins that same room as a second
connection, publishes a JSON command on the ``desktop-cmd`` topic, and awaits
the matching reply on ``desktop-result`` (correlated by ``id``). No tunnel or
public hostname is needed: every party connects OUT to LiveKit.

This is the server-side ``ToolRegistry`` counterpart of the regex fallback in
``livekit/worker.py``. The worker-side ``@function_tool`` port was reverted
(commit ``bc60417``) because ``server/stream_bridge.py`` filters a request's
tools against this registry — so desktop control must live here to be
reachable through the agent stream.

Requires, on the OpenJarvis backend service:
  - env: ``LIVEKIT_URL``, ``LIVEKIT_API_KEY``, ``LIVEKIT_API_SECRET``
          (and optionally ``JARVIS_CONTROL_ROOM``, default ``jarvis-control``)
  - the ``livekit`` realtime SDK (pyproject ``server`` extra)
The tool degrades gracefully — a clear ``ToolResult(success=False)`` — when
either is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_CONTROL_ROOM = os.environ.get("JARVIS_CONTROL_ROOM", "jarvis-control")
_TOPIC_CMD = "desktop-cmd"
_TOPIC_RESULT = "desktop-result"
_VALID_ACTIONS = (
    "open", "list_dir", "read_file", "write_file", "make_dir",
    "run_command", "set_volume", "delete", "empty_recycle_bin",
    "move", "copy", "search_files", "system_status", "list_processes",
    "close_app", "media_key", "play_media", "lock_workstation",
)


def _norm_machine(machine: str) -> str:
    """Map a free-form machine word to a bridge label ('laptop'/'rog'/'all')."""
    m = (machine or "").strip().lower()
    if "rog" in m:
        return "rog"
    if m in ("all", "both", "every"):
        return "all"
    return "laptop"  # default — covers 'laptop', 'desktop', 'pc', '' etc.


class _DesktopBridgeSession:
    """Per-process LiveKit connection to the desktop-bridge control room.

    ``ToolRegistry`` tools run synchronously (the ``ToolExecutor`` dispatches
    ``execute()`` in a thread pool), but the LiveKit room is async and must
    persist between calls. So this owns a dedicated daemon thread running its
    own asyncio loop; ``submit()`` is the sync entry point and marshals the
    coroutine onto that loop.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._room: Any = None  # rtc.Room — created lazily on the bg loop
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock: asyncio.Lock | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(
            target=_run, name="desktop-bridge-loop", daemon=True
        ).start()
        self._loop = loop
        return loop

    async def _ensure_room(self) -> Any:
        # Imported here so the package loads even when `livekit` (the realtime
        # SDK) is absent — only callers of the tool need it.
        from livekit import api, rtc

        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._room is not None:
                return self._room
            url = os.environ.get("LIVEKIT_URL", "")
            key = os.environ.get("LIVEKIT_API_KEY", "")
            secret = os.environ.get("LIVEKIT_API_SECRET", "")
            if not (url and key and secret):
                raise RuntimeError(
                    "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET "
                    "are not set on the OpenJarvis backend service"
                )
            token = (
                api.AccessToken(key, secret)
                .with_identity(f"openjarvis-backend-ctl-{uuid.uuid4().hex[:8]}")
                .with_grants(
                    api.VideoGrants(
                        room_join=True,
                        room=_CONTROL_ROOM,
                        can_publish=True,
                        can_subscribe=True,
                        can_publish_data=True,
                    )
                )
                .to_jwt()
            )
            room = rtc.Room()

            @room.on("data_received")
            def _on_data(packet: "rtc.DataPacket") -> None:  # noqa: ANN001
                if packet.topic != _TOPIC_RESULT:
                    return
                try:
                    msg = json.loads(bytes(packet.data).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return
                fut = self._pending.pop(msg.get("id", ""), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

            await room.connect(url, token)
            self._room = room
            logger.info("desktop bridge: connected to '%s'", _CONTROL_ROOM)
            return room

    async def _send(
        self, target: str, cmd: str, args: dict, timeout: float
    ) -> dict:
        room = await self._ensure_room()
        cmd_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        payload = json.dumps(
            {"id": cmd_id, "target": target, "cmd": cmd, "args": args}
        ).encode("utf-8")
        try:
            await room.local_participant.publish_data(
                payload, reliable=True, topic=_TOPIC_CMD
            )
            msg = await asyncio.wait_for(fut, timeout)
            return msg.get("result", {})
        except asyncio.TimeoutError:
            return {
                "error": (
                    f"no response from the '{target}' machine — is its "
                    "desktop-bridge running?"
                )
            }
        finally:
            self._pending.pop(cmd_id, None)

    def submit(
        self, target: str, cmd: str, args: dict, timeout: float = 30.0
    ) -> dict:
        """Sync entry point — run the async send on the background loop."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._send(target, cmd, args, timeout), loop
        )
        # A little headroom over the coroutine's own asyncio.wait_for timeout.
        return future.result(timeout=timeout + 10.0)


_bridge = _DesktopBridgeSession()


@ToolRegistry.register("desktop_control")
class DesktopControlTool(BaseTool):
    """Operate the user's Windows machines (laptop + ROG) over LiveKit."""

    tool_id = "desktop_control"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="desktop_control",
            description=(
                "Operate one of the user's Windows machines — their laptop "
                "or their ROG desktop. Open an app, file, folder or URL; "
                "list a directory; read a text file; run a shell command; "
                "or change the system volume. The target machine must have "
                "the desktop-bridge program running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "machine": {
                        "type": "string",
                        "enum": ["laptop", "rog", "all"],
                        "description": "Which machine to act on.",
                    },
                    "action": {
                        "type": "string",
                        "enum": list(_VALID_ACTIONS),
                        "description": (
                            "open = launch an app/file/folder/URL; "
                            "list_dir = list a folder's contents; "
                            "read_file = read a text file; "
                            "write_file = write text to a file; "
                            "make_dir = create a directory; "
                            "delete = move file/folder to Recycle Bin; "
                            "empty_recycle_bin = clear the Recycle Bin; "
                            "move/copy = move or copy file/folder; "
                            "search_files = find files by name; "
                            "system_status = CPU/RAM/disk snapshot; "
                            "list_processes = top processes by RAM/CPU; "
                            "close_app = terminate a running app; "
                            "set_volume = change the system volume; "
                            "media_key = play/pause/next/prev/stop; "
                            "play_media = find and play a song/video; "
                            "lock_workstation = lock the screen; "
                            "run_command = last-resort PowerShell."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "For action 'open': the app name (e.g. "
                            "'notepad'), a file/folder path, or a URL."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "For 'list_dir' or 'read_file': the directory "
                            "or file path."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": "For 'run_command': the shell command.",
                    },
                    "level": {
                        "type": "integer",
                        "description": (
                            "For 'set_volume': an absolute volume level "
                            "0-100. Omit to use 'direction' instead."
                        ),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "mute", "unmute"],
                        "description": (
                            "For 'set_volume' without a 'level': nudge the "
                            "volume up/down, or mute/unmute."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "For 'write_file': the text to write.",
                    },
                    "src": {
                        "type": "string",
                        "description": "For 'move'/'copy': source path.",
                    },
                    "dst": {
                        "type": "string",
                        "description": "For 'move'/'copy': destination path.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "For 'search_files': filename substring to "
                            "match (case-insensitive)."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "For 'close_app': process name to terminate "
                            "(e.g. 'chrome', 'notepad')."
                        ),
                    },
                    "top": {
                        "type": "integer",
                        "description": (
                            "For 'list_processes': how many top entries "
                            "to return (default 10)."
                        ),
                    },
                    "by": {
                        "type": "string",
                        "enum": ["memory", "cpu"],
                        "description": (
                            "For 'list_processes': sort key (default memory)."
                        ),
                    },
                    "key": {
                        "type": "string",
                        "enum": ["play_pause", "next", "previous", "stop"],
                        "description": (
                            "For 'media_key': which transport key to tap."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "For 'play_media': the song/video name to find."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "For 'search_files': max matches to return "
                            "(default 50)."
                        ),
                    },
                },
                "required": ["machine", "action"],
            },
            category="system",
            # No confirmation gate: this is invoked from the voice/agent
            # stream, which has no interactive confirm callback — gating it
            # would make every call fail. The desktop-bridge itself only
            # runs shell commands when started with JARVIS_BRIDGE_ALLOW_SHELL.
            requires_confirmation=False,
            timeout_seconds=60.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        machine = _norm_machine(params.get("machine", ""))
        action = (params.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            return ToolResult(
                tool_name="desktop_control",
                content=(
                    f"Unknown action '{action}'. Valid actions: "
                    + ", ".join(_VALID_ACTIONS)
                ),
                success=False,
            )

        # Map the tool's action/params onto the desktop-bridge command set
        # (open / list_dir / read_file / shell).
        if action == "open":
            target = params.get("target") or params.get("path") or ""
            if not target:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'open' action needs a 'target'.",
                    success=False,
                )
            cmd, args, timeout = "open", {"target": target}, 30.0
        elif action == "list_dir":
            path = params.get("path") or params.get("target") or ""
            if not path:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'list_dir' action needs a 'path'.",
                    success=False,
                )
            cmd, args, timeout = "list_dir", {"path": path}, 30.0
        elif action == "read_file":
            path = params.get("path") or params.get("target") or ""
            if not path:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'read_file' action needs a 'path'.",
                    success=False,
                )
            cmd, args, timeout = "read_file", {"path": path}, 30.0
        elif action == "write_file":
            path = params.get("path") or ""
            content = params.get("content") or ""
            if not path:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'write_file' action needs a 'path'.",
                    success=False,
                )
            cmd, args, timeout = (
                "write_file", {"path": path, "content": content}, 30.0
            )
        elif action == "make_dir":
            path = params.get("path") or ""
            if not path:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'make_dir' action needs a 'path'.",
                    success=False,
                )
            cmd, args, timeout = "make_dir", {"path": path}, 30.0
        elif action == "delete":
            path = params.get("path") or params.get("target") or ""
            if not path:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'delete' action needs a 'path'.",
                    success=False,
                )
            cmd, args, timeout = "delete", {"path": path}, 30.0
        elif action == "empty_recycle_bin":
            cmd, args, timeout = "empty_recycle_bin", {}, 60.0
        elif action in ("move", "copy"):
            src = params.get("src") or ""
            dst = params.get("dst") or ""
            if not (src and dst):
                return ToolResult(
                    tool_name="desktop_control",
                    content=f"The '{action}' action needs 'src' and 'dst'.",
                    success=False,
                )
            cmd, args, timeout = action, {"src": src, "dst": dst}, 60.0
        elif action == "search_files":
            pattern = (params.get("pattern") or params.get("name") or "").strip()
            if not pattern:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'search_files' action needs a 'pattern'.",
                    success=False,
                )
            cmd, args, timeout = "search_files", {
                "path": params.get("path") or "~",
                "pattern": pattern,
                "limit": int(params.get("limit") or 50),
            }, 60.0
        elif action == "system_status":
            cmd, args, timeout = "system_status", {}, 30.0
        elif action == "list_processes":
            cmd, args, timeout = "list_processes", {
                "top": int(params.get("top") or 10),
                "by": (params.get("by") or "memory"),
            }, 30.0
        elif action == "close_app":
            name = (params.get("name") or params.get("target") or "").strip()
            if not name:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'close_app' action needs a 'name'.",
                    success=False,
                )
            cmd, args, timeout = "close_app", {"name": name}, 30.0
        elif action == "media_key":
            key = (params.get("key") or "").strip().lower()
            if not key:
                return ToolResult(
                    tool_name="desktop_control",
                    content=(
                        "The 'media_key' action needs a 'key' "
                        "(play_pause/next/previous/stop)."
                    ),
                    success=False,
                )
            cmd, args, timeout = "media_key", {"key": key}, 10.0
        elif action == "play_media":
            query = (params.get("query") or params.get("target") or "").strip()
            if not query:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'play_media' action needs a 'query'.",
                    success=False,
                )
            cmd, args, timeout = "play_media", {"query": query}, 30.0
        elif action == "lock_workstation":
            cmd, args, timeout = "lock_workstation", {}, 10.0
        elif action == "set_volume":
            level = params.get("level")
            direction = (params.get("direction") or "").strip().lower()
            if level is not None:
                vol_args: dict = {
                    "action": "set",
                    "level": max(0, min(100, int(level))),
                }
            elif direction in ("up", "down", "mute", "unmute"):
                vol_args = {"action": direction}
            else:
                return ToolResult(
                    tool_name="desktop_control",
                    content=(
                        "The 'set_volume' action needs a 'level' (0-100) "
                        "or a 'direction' (up/down/mute/unmute)."
                    ),
                    success=False,
                )
            cmd, args, timeout = "volume", vol_args, 30.0
        else:  # run_command
            command = params.get("command") or params.get("target") or ""
            if not command:
                return ToolResult(
                    tool_name="desktop_control",
                    content="The 'run_command' action needs a 'command'.",
                    success=False,
                )
            cmd, args, timeout = "shell", {"command": command}, 70.0

        try:
            result = _bridge.submit(machine, cmd, args, timeout=timeout)
        except ImportError:
            return ToolResult(
                tool_name="desktop_control",
                content=(
                    "Desktop control unavailable: the 'livekit' package is "
                    "not installed on the server (add it to the pyproject "
                    "'server' extra)."
                ),
                success=False,
            )
        except RuntimeError as exc:
            return ToolResult(
                tool_name="desktop_control",
                content=f"Desktop control unavailable: {exc}",
                success=False,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool_name="desktop_control",
                content=f"Desktop control error: {exc}",
                success=False,
            )

        if isinstance(result, dict) and result.get("error"):
            return ToolResult(
                tool_name="desktop_control",
                content=str(result["error"]),
                success=False,
                metadata={"machine": machine, "action": action},
            )
        content = result if isinstance(result, str) else json.dumps(result)
        return ToolResult(
            tool_name="desktop_control",
            content=content or "(done)",
            success=True,
            metadata={"machine": machine, "action": action},
        )


__all__ = ["DesktopControlTool"]
