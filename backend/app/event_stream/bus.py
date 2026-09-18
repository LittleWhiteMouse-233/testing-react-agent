"""进程内按流隔离的有界广播；慢订阅断开，历史恢复由消费者负责。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator


class EventBus[T]:
    """None 表示队列溢出后的订阅结束，不阻塞业务发布者。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[T | None]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(
        self, stream_id: str
    ) -> AsyncGenerator[asyncio.Queue[T | None], None]:
        queue: asyncio.Queue[T | None] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[stream_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(stream_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(stream_id, None)

    async def publish(self, stream_id: str, event: T) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(stream_id, ()))
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers[stream_id].discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
