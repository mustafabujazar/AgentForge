"""In-process publish/subscribe broker for live run events (SSE).

The durable event log lives in the ``run_events`` table.  This broker only carries
the *live tail* to connected SSE clients.  The orchestrator runs blocking LLM and
tool calls in a worker thread, so it publishes via :meth:`publish_threadsafe`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Bind the broker to the serving event loop (called from app lifespan)."""
        self._loop = loop or asyncio.get_running_loop()

    def _ensure_queue(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers[run_id].add(queue)
        return queue

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        queue = self._ensure_queue(run_id)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            with self._lock:
                self._subscribers.get(run_id, set()).discard(queue)

    def _put(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers.get(run_id, ()))
        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest item and retry once.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:  # pragma: no cover - defensive
                    logger.warning("dropped event for slow subscriber", extra={"run_id": run_id})

    def publish_threadsafe(self, run_id: str, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._put, run_id, event)
        else:
            self._put(run_id, event)

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        self._put(run_id, event)

    def subscriber_count(self, run_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(run_id, set()))

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()


broker = EventBroker()
