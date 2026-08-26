"""In-process event bus bridging background threads to SSE subscribers.

The poller runs on APScheduler worker threads while the SSE endpoint lives on
the asyncio loop, so every publish is marshalled onto the loop with
``call_soon_threadsafe``. Subscribers get a bounded queue: a browser tab that
stops reading gets its oldest events dropped rather than growing memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

MAX_QUEUE = 100


# eq=False keeps identity hashing, which is what lets subscribers live in a
# set. The generated __eq__ would otherwise make the class unhashable.
@dataclass(eq=False)
class Subscriber:
    user_id: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[Subscriber]] = defaultdict(set)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, user_id: int) -> Subscriber:
        sub = Subscriber(user_id=user_id)
        with self._lock:
            self._subscribers[user_id].add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subscribers.get(sub.user_id, set()).discard(sub)

    def subscriber_count(self, user_id: int | None = None) -> int:
        with self._lock:
            if user_id is None:
                return sum(len(v) for v in self._subscribers.values())
            return len(self._subscribers.get(user_id, set()))

    def publish(self, user_id: int, event: str, data: dict[str, Any]) -> None:
        """Thread-safe publish. Safe to call before any client connected."""
        payload = {
            "event": event,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            targets = list(self._subscribers.get(user_id, set()))
        if not targets:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for sub in targets:
            loop.call_soon_threadsafe(self._offer, sub, payload)

    def broadcast(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            user_ids = list(self._subscribers)
        for user_id in user_ids:
            self.publish(user_id, event, data)

    @staticmethod
    def _offer(sub: Subscriber, payload: dict[str, Any]) -> None:
        try:
            sub.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop the oldest event so a stalled tab cannot grow unbounded.
            try:
                sub.queue.get_nowait()
                sub.queue.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                log.debug("Dropped SSE event for user %s", sub.user_id)


bus = EventBus()


def format_sse(payload: dict[str, Any]) -> str:
    body = json.dumps(payload.get("data", {}), default=str)
    return f"event: {payload.get('event', 'message')}\ndata: {body}\n\n"
