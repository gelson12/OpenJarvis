"""Server-side ledger of open resources for OpenJarvis.

Tracks widgets, the live Playwright browser session, and any future
tracked resource (e.g. desktop apps) so the worker can answer
"what's open?" and auto-close idle resources with a polite spoken
warning. Each resource carries an optional idle threshold (None =
never auto-close). The watcher coroutine scans every 30 s; on the
first observed over-threshold idleness it speaks
"I'll close X, sir — it's been idle for N minutes", waits 10 s
grace, then invokes the resource's on_close handler.

The ledger is per-session (owned by Assistant). Re-creating Assistant
also recreates the ledger; nothing here is persisted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

CloseHandler = Callable[[], Awaitable[None]]


@dataclass
class Resource:
    kind: str  # 'widget' | 'browser' | 'cti' | 'desktop_app' | ...
    id: str
    label: str  # spoken: "YouTube panel", "Notepad on laptop"
    opened_at: float  # time.monotonic()
    last_activity_at: float
    idle_threshold_s: Optional[float] = None  # None = never auto-close
    machine: Optional[str] = None
    on_close: Optional[CloseHandler] = None
    warned: bool = False
    grace_started_at: Optional[float] = None


class ResourceLedger:
    """Inventory + idle-watcher for everything OpenJarvis has opened."""

    GRACE_SECONDS = 10.0
    SCAN_INTERVAL = 30.0

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Resource] = {}
        self._lock = asyncio.Lock()
        self._watcher: Optional[asyncio.Task] = None

    @staticmethod
    def _key(kind: str, id_: str) -> tuple[str, str]:
        return (kind, id_)

    async def open(
        self,
        kind: str,
        id_: str,
        label: str,
        *,
        idle_threshold_s: Optional[float] = None,
        machine: Optional[str] = None,
        on_close: Optional[CloseHandler] = None,
    ) -> None:
        """Register a newly-opened resource (or refresh an existing one)."""
        async with self._lock:
            now = time.monotonic()
            key = self._key(kind, id_)
            existing = self._items.get(key)
            if existing is not None:
                # Re-open touches activity but preserves opened_at.
                existing.last_activity_at = now
                existing.warned = False
                existing.grace_started_at = None
                if label:
                    existing.label = label
                return
            self._items[key] = Resource(
                kind=kind,
                id=id_,
                label=label,
                opened_at=now,
                last_activity_at=now,
                idle_threshold_s=idle_threshold_s,
                machine=machine,
                on_close=on_close,
            )
            logger.info("ledger: opened %s/%s (%s)", kind, id_, label)

    async def touch(self, kind: str, id_: str) -> None:
        """Update last_activity_at — resets any pending idle warning."""
        async with self._lock:
            r = self._items.get(self._key(kind, id_))
            if r is None:
                return
            r.last_activity_at = time.monotonic()
            r.warned = False
            r.grace_started_at = None

    async def close(self, kind: str, id_: str) -> bool:
        """Remove a resource and invoke its on_close handler (best-effort).

        Idempotent: returns False if the resource was not tracked.
        on_close runs OUTSIDE the lock so it may re-enter the ledger
        (e.g. _stop_browser calling ledger.close('browser','main') again
        is safe — the entry is already gone).
        """
        async with self._lock:
            r = self._items.pop(self._key(kind, id_), None)
        if r is None:
            return False
        logger.info("ledger: closed %s/%s", kind, id_)
        if r.on_close is not None:
            try:
                await r.on_close()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "ledger: on_close for %s/%s raised: %s",
                    kind,
                    id_,
                    exc,
                )
        return True

    async def list_open(self) -> list[Resource]:
        async with self._lock:
            return list(self._items.values())

    async def has(self, kind: str, id_: str) -> bool:
        async with self._lock:
            return self._key(kind, id_) in self._items

    async def start_idle_watcher(self, session) -> None:
        """Start the background idle watcher (idempotent)."""
        if self._watcher is not None and not self._watcher.done():
            return
        self._watcher = asyncio.create_task(self._watch(session))
        logger.info("ledger: idle watcher started")

    async def stop_idle_watcher(self) -> None:
        task = self._watcher
        self._watcher = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _watch(self, session) -> None:
        try:
            while True:
                await asyncio.sleep(self.SCAN_INTERVAL)
                await self._scan_once(session)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("ledger watcher crashed: %s", exc)

    async def _scan_once(self, session) -> None:
        now = time.monotonic()
        to_warn: list[Resource] = []
        to_close: list[Resource] = []
        async with self._lock:
            for r in self._items.values():
                if r.idle_threshold_s is None:
                    continue
                idle = now - r.last_activity_at
                if idle < r.idle_threshold_s:
                    # Activity returned before the close fired — clear any
                    # pending warning state.
                    r.warned = False
                    r.grace_started_at = None
                    continue
                if not r.warned:
                    r.warned = True
                    r.grace_started_at = now
                    to_warn.append(r)
                elif r.grace_started_at is not None and (
                    now - r.grace_started_at >= self.GRACE_SECONDS
                ):
                    to_close.append(r)
        # Side-effects (TTS + close handlers) happen OUTSIDE the lock.
        for r in to_warn:
            mins = max(1, int(round((now - r.last_activity_at) / 60.0)))
            try:
                await session.say(
                    f"I'll close {r.label}, sir — it's been idle for "
                    f"{mins} minute{'s' if mins != 1 else ''}."
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("ledger warning say failed: %s", exc)
        for r in to_close:
            await self.close(r.kind, r.id)
