"""OpenCTI integration — Jarvis's intelligence/investigation layer.

Wraps a laptop-hosted OpenCTI instance (https://github.com/OpenCTI-Platform/opencti)
through its GraphQL API. OpenCTI runs locally on the user's laptop
(Docker on http://localhost:8080); to reach it from the cloud backend
without punching the laptop onto the public internet we proxy every
HTTP call over the SAME LiveKit data channel the desktop-bridge already
listens on — the bridge runs the request against localhost and ships
the response back. Zero new infra, zero public exposure of OpenCTI.

Operations cover the day-to-day analyst loop: search the knowledge
graph, log new observables, open incidents, link entities, and
summarise what came in over the last N hours.

This is the server-side counterpart of the LiveKit worker's
``OpenCTIClient``. Same shape, different sync/async glue:
``desktop_bridge.py``'s ``_DesktopBridgeSession`` already proves we can
own a long-lived LiveKit room from a sync ``execute()`` via a daemon
thread + asyncio loop; we use the same trick to proxy HTTP.

Required env (on the OpenJarvis backend service):
  - ``OPENCTI_URL``              the URL the *bridge* hits, default
                                 ``http://localhost:8080``
  - ``OPENCTI_TOKEN``            admin/api token (required)
  - ``OPENCTI_BRIDGE_MACHINE``   which desktop-bridge runs OpenCTI
                                 (default ``laptop``)
  - ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET``
                                 to join the desktop-bridge control room

This tool degrades to ``ToolResult(success=False)`` with a clear message
when env vars are missing or the bridge is offline, rather than raising.
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


_VALID_ACTIONS = (
    "search",
    "add_observable",
    "create_incident",
    "link",
    "summary",
    "list_indicators",
)

# Spoken aliases for OpenCTI's STIX observable types — the LLM/voice
# layer should pass these natural names; the tool maps them to the exact
# STIX names OpenCTI expects.
_OBSERVABLE_TYPE_MAP: dict[str, str] = {
    "domain": "Domain-Name",
    "domain-name": "Domain-Name",
    "hostname": "Hostname",
    "ip": "IPv4-Addr",
    "ipv4": "IPv4-Addr",
    "ipv6": "IPv6-Addr",
    "url": "Url",
    "email": "Email-Addr",
    "email-addr": "Email-Addr",
    "md5": "StixFile",
    "sha1": "StixFile",
    "sha256": "StixFile",
    "hash": "StixFile",
    "file": "StixFile",
    "user-agent": "User-Agent",
    "mutex": "Mutex",
    "registry-key": "Windows-Registry-Key",
}


def _normalise_observable_type(t: str) -> str:
    return _OBSERVABLE_TYPE_MAP.get((t or "").strip().lower(), t or "")


_CONTROL_ROOM = os.environ.get("JARVIS_CONTROL_ROOM", "jarvis-control")
_TOPIC_CMD = "desktop-cmd"
_TOPIC_RESULT = "desktop-result"


class _OpenCTIClient:
    """Bridge-proxied OpenCTI client owning a LiveKit room on a bg loop.

    ToolRegistry tools dispatch ``execute()`` from a thread pool — but
    LiveKit ``rtc.Room`` is async and needs a persistent event loop. So
    we own a daemon thread running its own asyncio loop, join the
    desktop-bridge control room once, and proxy every GraphQL call over
    its data channel. Mirrors ``desktop_bridge.py``'s
    ``_DesktopBridgeSession`` design.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._room: Any = None  # rtc.Room — created lazily on bg loop
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock: asyncio.Lock | None = None
        self._base_url = os.environ.get(
            "OPENCTI_URL", "http://localhost:8080"
        ).rstrip("/")
        self._token = os.environ.get("OPENCTI_TOKEN", "")
        self._machine = os.environ.get(
            "OPENCTI_BRIDGE_MACHINE", "laptop"
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(
            target=_run, name="opencti-loop", daemon=True
        ).start()
        self._loop = loop
        return loop

    async def _ensure_room(self) -> Any:
        # Lazy import so a stripped-down deployment without `livekit`
        # still imports this module cleanly (the executor returns a
        # clear error instead of failing at import time).
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
            if not self._token:
                raise RuntimeError(
                    "OPENCTI_TOKEN is not set on the OpenJarvis backend "
                    "service"
                )
            token = (
                api.AccessToken(key, secret)
                .with_identity(f"openjarvis-cti-{uuid.uuid4().hex[:8]}")
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
            logger.info(
                "opencti: bridge-proxy joined '%s' (target=%s, url=%s)",
                _CONTROL_ROOM, self._machine, self._base_url,
            )
            return room

    def _online_machines(self) -> list[str]:
        if self._room is None:
            return []
        ms: set[str] = set()
        for p in self._room.remote_participants.values():
            ident = getattr(p, "identity", "") or ""
            if ident.startswith("desktop-bridge-"):
                ms.add(ident[len("desktop-bridge-"):].strip().lower())
        return sorted(m for m in ms if m)

    async def _proxy_send(self, args: dict, timeout: float) -> dict:
        room = await self._ensure_room()
        if self._machine not in self._online_machines():
            raise RuntimeError(
                f"the '{self._machine}' bridge is offline — start "
                "desktop-bridge\\run.bat on that machine"
            )
        cmd_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        payload = json.dumps(
            {
                "id": cmd_id,
                "target": self._machine,
                "cmd": "http_proxy",
                "args": args,
            }
        ).encode("utf-8")
        try:
            await room.local_participant.publish_data(
                payload, reliable=True, topic=_TOPIC_CMD
            )
            msg = await asyncio.wait_for(fut, timeout)
            return msg.get("result", {}) or {}
        except asyncio.TimeoutError:
            return {
                "error": (
                    f"no response from the '{self._machine}' bridge"
                )
            }
        finally:
            self._pending.pop(cmd_id, None)

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        res = await self._proxy_send(
            {
                "base_url": self._base_url,
                "method": "POST",
                "path": "/graphql",
                "headers": {
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                "body": {"query": query, "variables": variables or {}},
                "timeout": 30.0,
            },
            timeout=40.0,
        )
        if not isinstance(res, dict) or res.get("error"):
            err = (
                res.get("error")
                if isinstance(res, dict) else "no dict"
            )
            raise RuntimeError(f"bridge proxy error: {err}")
        status = res.get("status")
        payload = res.get("json")
        if payload is None:
            try:
                payload = json.loads(res.get("body") or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
        if status and status >= 400:
            raise RuntimeError(
                f"opencti HTTP {status}: {(res.get('body') or '')[:200]}"
            )
        if isinstance(payload, dict) and payload.get("errors"):
            raise RuntimeError(
                f"opencti graphql errors: {payload['errors'][:2]}"
            )
        return (
            payload.get("data") or {} if isinstance(payload, dict) else {}
        )

    def submit(self, coro_fn, timeout: float = 30.0) -> dict:
        """Sync entry point — run a coroutine factory on the bg loop."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro_fn(), loop)
        return future.result(timeout=timeout + 10.0)


_client = _OpenCTIClient()


# ── GraphQL operations ───────────────────────────────────────────────


async def _op_search(query: str, limit: int = 10) -> dict:
    # Searches across STIX core objects — entities, observables,
    # indicators, incidents, reports, threat actors, malware, etc.
    gql = """
    query Search($search: String, $count: Int) {
      stixCoreObjects(search: $search, first: $count) {
        edges {
          node {
            id
            entity_type
            standard_id
            ... on StixDomainObject {
              created
              modified
            }
            representative {
              main
              secondary
            }
          }
        }
      }
    }
    """
    data = await _client._gql(gql, {"search": query, "count": limit})
    edges = (data.get("stixCoreObjects") or {}).get("edges") or []
    hits = []
    for e in edges:
        n = e.get("node") or {}
        rep = n.get("representative") or {}
        hits.append(
            {
                "id": n.get("id"),
                "type": n.get("entity_type"),
                "name": rep.get("main") or rep.get("secondary") or "",
            }
        )
    return {"matches": hits, "count": len(hits), "query": query}


async def _op_add_observable(value: str, stix_type: str) -> dict:
    # Single-flight observable creation. We rely on OpenCTI's upsert
    # behaviour — repeat calls with the same value return the existing
    # object instead of duplicating.
    obs_type = _normalise_observable_type(stix_type)
    if not obs_type:
        raise RuntimeError(f"unknown observable type '{stix_type}'")

    # The input field name depends on the type — OpenCTI's StixCyberObservableAddInput
    # is a union with one named field per type. Map the canonical STIX type
    # name to its input key.
    type_to_input_key: dict[str, str] = {
        "Domain-Name": "DomainName",
        "Hostname": "Hostname",
        "IPv4-Addr": "IPv4Addr",
        "IPv6-Addr": "IPv6Addr",
        "Url": "Url",
        "Email-Addr": "EmailAddr",
        "StixFile": "StixFile",
        "User-Agent": "UserAgent",
        "Mutex": "Mutex",
        "Windows-Registry-Key": "WindowsRegistryKey",
    }
    key = type_to_input_key.get(obs_type, obs_type.replace("-", ""))

    # The value field on the input is named "value" for most types but
    # "key" for registry, "name" for files — keep it simple and pass
    # "value" by default; OpenCTI will reject if mis-shaped.
    inner: dict[str, Any]
    if obs_type == "StixFile":
        inner = {"name": value, "hashes": [{"algorithm": "Unknown", "hash": value}]}
    elif obs_type == "Windows-Registry-Key":
        inner = {"attribute_key": value}
    else:
        inner = {"value": value}

    gql = f"""
    mutation AddObs($input: StixCyberObservableAddInput!) {{
      stixCyberObservableAdd(input: $input) {{
        id
        observable_value
        entity_type
      }}
    }}
    """
    variables = {
        "input": {
            "type": obs_type,
            key: inner,
        }
    }
    data = await _client._gql(gql, variables)
    obs = data.get("stixCyberObservableAdd") or {}
    return {
        "id": obs.get("id"),
        "value": obs.get("observable_value") or value,
        "type": obs.get("entity_type") or obs_type,
    }


async def _op_create_incident(name: str, description: str = "") -> dict:
    gql = """
    mutation IncAdd($input: IncidentAddInput!) {
      incidentAdd(input: $input) {
        id
        name
        created
      }
    }
    """
    data = await _client._gql(
        gql, {"input": {"name": name, "description": description}}
    )
    inc = data.get("incidentAdd") or {}
    return {"id": inc.get("id"), "name": inc.get("name")}


async def _op_link(
    from_id: str, to_id: str, relationship: str = "related-to"
) -> dict:
    gql = """
    mutation RelAdd($input: StixCoreRelationshipAddInput!) {
      stixCoreRelationshipAdd(input: $input) {
        id
        relationship_type
      }
    }
    """
    data = await _client._gql(
        gql,
        {
            "input": {
                "fromId": from_id,
                "toId": to_id,
                "relationship_type": relationship,
            }
        },
    )
    rel = data.get("stixCoreRelationshipAdd") or {}
    return {
        "id": rel.get("id"),
        "relationship": rel.get("relationship_type") or relationship,
    }


async def _op_summary(hours: int = 24) -> dict:
    # Lightweight rollup: counts of new core objects in the window, plus
    # the most recent few names. A real "what happened today" digest.
    gql = """
    query Summary($filters: FilterGroup) {
      stixCoreObjects(filters: $filters, first: 8, orderBy: created_at, orderMode: desc) {
        pageInfo { globalCount }
        edges {
          node {
            entity_type
            representative { main }
          }
        }
      }
    }
    """
    # ISO 8601 cutoff `hours` ago.
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    filters = {
        "mode": "and",
        "filters": [
            {"key": "created_at", "operator": "gt", "values": [cutoff]}
        ],
        "filterGroups": [],
    }
    data = await _client._gql(gql, {"filters": filters})
    block = data.get("stixCoreObjects") or {}
    total = (block.get("pageInfo") or {}).get("globalCount", 0)
    edges = block.get("edges") or []
    items = []
    for e in edges:
        n = e.get("node") or {}
        items.append(
            {
                "type": n.get("entity_type"),
                "name": (n.get("representative") or {}).get("main", ""),
            }
        )
    return {"window_hours": hours, "total": total, "recent": items}


async def _op_list_indicators(limit: int = 10) -> dict:
    gql = """
    query Inds($count: Int) {
      indicators(first: $count, orderBy: created_at, orderMode: desc) {
        edges {
          node {
            id
            name
            pattern
            x_opencti_score
            valid_from
          }
        }
      }
    }
    """
    data = await _client._gql(gql, {"count": limit})
    edges = (data.get("indicators") or {}).get("edges") or []
    items = []
    for e in edges:
        n = e.get("node") or {}
        items.append(
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "score": n.get("x_opencti_score"),
            }
        )
    return {"indicators": items, "count": len(items)}


# ── ToolRegistry surface ─────────────────────────────────────────────


@ToolRegistry.register("opencti_control")
class OpenCTIControlTool(BaseTool):
    """Operate the user's OpenCTI intelligence platform over GraphQL."""

    tool_id = "opencti_control"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="opencti_control",
            description=(
                "Query and update the user's self-hosted OpenCTI threat-"
                "intelligence platform. Search the knowledge graph, log "
                "new observables (domains, IPs, hashes, URLs, emails), "
                "open incidents, link entities with STIX relationships, "
                "summarise what came in over the last N hours, and list "
                "recent indicators."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_VALID_ACTIONS),
                        "description": (
                            "search = search across all STIX objects; "
                            "add_observable = create an observable "
                            "(needs value + observable_type); "
                            "create_incident = open a new incident "
                            "(needs name, optional description); "
                            "link = relate two STIX objects by id "
                            "(needs from_id, to_id, relationship); "
                            "summary = digest of the last N hours; "
                            "list_indicators = recent indicators."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "For 'search': the search string."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "For 'add_observable': the observable value "
                            "(e.g. 'foo.com', '8.8.8.8', a hash)."
                        ),
                    },
                    "observable_type": {
                        "type": "string",
                        "description": (
                            "For 'add_observable': domain, ip, ipv6, "
                            "url, email, md5, sha1, sha256, hash, "
                            "user-agent, mutex, registry-key."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "For 'create_incident': incident name.",
                    },
                    "description": {
                        "type": "string",
                        "description": "For 'create_incident': free-text description.",
                    },
                    "from_id": {
                        "type": "string",
                        "description": "For 'link': source STIX object id.",
                    },
                    "to_id": {
                        "type": "string",
                        "description": "For 'link': target STIX object id.",
                    },
                    "relationship": {
                        "type": "string",
                        "description": (
                            "For 'link': STIX relationship type "
                            "(default 'related-to'; e.g. 'indicates', "
                            "'attributed-to', 'uses', 'targets')."
                        ),
                    },
                    "hours": {
                        "type": "integer",
                        "description": "For 'summary': window length in hours (default 24).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "For 'search' / 'list_indicators': max results "
                            "(default 10)."
                        ),
                    },
                },
                "required": ["action"],
            },
            category="intelligence",
            requires_confirmation=False,
            timeout_seconds=60.0,
        )

    def execute(self, **params: Any) -> Any:
        action = (params.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            return ToolResult(
                tool_name="opencti_control",
                content=(
                    f"Unknown action '{action}'. Valid: "
                    + ", ".join(_VALID_ACTIONS)
                ),
                success=False,
            )

        try:
            if action == "search":
                query = (params.get("query") or "").strip()
                if not query:
                    return ToolResult(
                        tool_name="opencti_control",
                        content="The 'search' action needs a 'query'.",
                        success=False,
                    )
                limit = int(params.get("limit") or 10)
                result = _client.submit(lambda: _op_search(query, limit))
            elif action == "add_observable":
                value = (params.get("value") or "").strip()
                obs_type = (params.get("observable_type") or "").strip()
                if not (value and obs_type):
                    return ToolResult(
                        tool_name="opencti_control",
                        content=(
                            "The 'add_observable' action needs 'value' "
                            "and 'observable_type'."
                        ),
                        success=False,
                    )
                result = _client.submit(
                    lambda: _op_add_observable(value, obs_type)
                )
            elif action == "create_incident":
                name = (params.get("name") or "").strip()
                if not name:
                    return ToolResult(
                        tool_name="opencti_control",
                        content="The 'create_incident' action needs a 'name'.",
                        success=False,
                    )
                desc = params.get("description") or ""
                result = _client.submit(
                    lambda: _op_create_incident(name, desc)
                )
            elif action == "link":
                from_id = (params.get("from_id") or "").strip()
                to_id = (params.get("to_id") or "").strip()
                if not (from_id and to_id):
                    return ToolResult(
                        tool_name="opencti_control",
                        content="The 'link' action needs 'from_id' and 'to_id'.",
                        success=False,
                    )
                rel = (params.get("relationship") or "related-to").strip()
                result = _client.submit(
                    lambda: _op_link(from_id, to_id, rel)
                )
            elif action == "summary":
                hours = int(params.get("hours") or 24)
                result = _client.submit(lambda: _op_summary(hours))
            else:  # list_indicators
                limit = int(params.get("limit") or 10)
                result = _client.submit(lambda: _op_list_indicators(limit))
        except ImportError:
            return ToolResult(
                tool_name="opencti_control",
                content=(
                    "OpenCTI tool unavailable: 'httpx' is not installed "
                    "in this Python environment."
                ),
                success=False,
            )
        except RuntimeError as exc:
            return ToolResult(
                tool_name="opencti_control",
                content=f"OpenCTI unavailable: {exc}",
                success=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("opencti_control %s failed", action)
            return ToolResult(
                tool_name="opencti_control",
                content=f"OpenCTI error: {exc}",
                success=False,
            )

        content = json.dumps(result, default=str)
        return ToolResult(
            tool_name="opencti_control",
            content=content,
            success=True,
            metadata={"action": action},
        )


__all__ = ["OpenCTIControlTool"]
