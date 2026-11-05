"""In-process event fan-out from worker threads to Server-Sent Event streams."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._lock = threading.Lock()

    def listen(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Call ``callback(project_id, event)`` for every event, in the publishing thread; it must
        return quickly and must not take the project's lock (publishers may hold it)."""
        with self._lock:
            self._listeners.append(callback)

    def subscribe(self, project_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.append((project_id, asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s[2] is not q]

    def publish(self, project_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = [s for s in self._subs if s[0] == project_id]
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(project_id, event)
            except Exception:  # noqa: BLE001 - a listener must never break the publisher
                log.exception("event listener failed")
        for _, loop, q in subs:
            try:
                loop.call_soon_threadsafe(_put, q, event)
            except RuntimeError:  # loop closed
                self.unsubscribe(q)


def _put(q: asyncio.Queue, event: dict) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass
