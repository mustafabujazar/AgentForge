"""Durable + live run event emission.

Every event is written to the ``run_events`` table with a monotonic per-run ``seq``
(the durable log replayed by the SSE endpoint) and simultaneously published to the
in-process :data:`app.core.events.broker` for the live tail.

Only one workflow thread emits events for a given run at a time, so ``seq`` is
allocated under a lock; if a concurrent writer ever wins an ``IntegrityError`` race
(the unique ``(run_id, seq)`` constraint) we re-sync from the database and retry.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.events import broker
from app.orchestrator.persistence import append_event, json_safe, max_event_seq

logger = logging.getLogger(__name__)


class EventEmitter:
    """Monotonic, durable event emitter for a single run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = max_event_seq(run_id)

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(
        self,
        event_type: str,
        *,
        agent: str | None = None,
        task_id: str | None = None,
        message: str = "",
        data: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist + publish one event.  Never raises (events must not break a run)."""
        payload_data = json_safe(data or {})
        with self._lock:
            self._seq += 1
            seq = self._seq
            payload = {
                "seq": seq,
                "run_id": self.run_id,
                "type": event_type,
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent": agent,
                "task_id": task_id,
                "message": (message or "")[:8000],
                "data": payload_data,
                "duration_ms": duration_ms,
            }
            for attempt in range(3):
                try:
                    append_event(
                        self.run_id,
                        seq=payload["seq"],
                        event_type=event_type,
                        agent=agent,
                        task_id=task_id,
                        message=message,
                        data=payload_data,
                        duration_ms=duration_ms,
                    )
                    break
                except IntegrityError:  # pragma: no cover - concurrent writer race
                    self._seq = max_event_seq(self.run_id) + 1
                    payload["seq"] = self._seq
                    logger.warning(
                        "event_seq_conflict",
                        extra={"run_id": self.run_id, "attempt": attempt, "new_seq": self._seq},
                    )
                except Exception:  # pragma: no cover - durable log must be best effort
                    logger.exception("event_persist_failed", extra={"run_id": self.run_id})
                    break
        try:
            broker.publish_threadsafe(self.run_id, payload)
        except Exception:  # pragma: no cover - live tail is best effort
            logger.exception("event_publish_failed", extra={"run_id": self.run_id})
        return payload


__all__ = ["EventEmitter"]
