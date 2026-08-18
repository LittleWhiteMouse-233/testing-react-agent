"""进程内可丢失的 TestRun 事件提交通知总线。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator


class EventBus:
    """只广播已提交 sequence；订阅者始终可从持久层补拉丢失通知。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[int]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(
        self, test_run_id: str
    ) -> AsyncGenerator[asyncio.Queue[int], None]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[test_run_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(test_run_id)
                if subscribers:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(test_run_id, None)

    async def publish(self, test_run_id: str, sequence: int) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(test_run_id, ()))
        for queue in subscribers:
            try:
                queue.put_nowait(sequence)
            except asyncio.QueueFull:
                # The client can recover every missed notification from SQLite.
                pass
