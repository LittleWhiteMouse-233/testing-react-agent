from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[int]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(
        self, run_id: str
    ) -> AsyncGenerator[asyncio.Queue[int], None]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(run_id)
                if subscribers:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(run_id, None)

    async def publish(self, run_id: str, sequence: int) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(run_id, ()))
        for queue in subscribers:
            try:
                queue.put_nowait(sequence)
            except asyncio.QueueFull:
                # The client can recover every missed notification from SQLite.
                pass
