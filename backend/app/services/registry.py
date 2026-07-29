from __future__ import annotations

import asyncio


class RunRegistry:
    def __init__(self) -> None:
        self._cancel: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, run_id: str, task: asyncio.Task[None]) -> asyncio.Event:
        event = asyncio.Event()
        self._cancel[run_id] = event
        self._tasks[run_id] = task
        return event

    def cancellation(self, run_id: str) -> asyncio.Event:
        return self._cancel.setdefault(run_id, asyncio.Event())

    def cancel(self, run_id: str) -> bool:
        event = self._cancel.get(run_id)
        if not event:
            return False
        event.set()
        return True

    def unregister(self, run_id: str) -> None:
        self._cancel.pop(run_id, None)
        self._tasks.pop(run_id, None)

    async def shutdown(self) -> None:
        tasks = tuple(self._tasks.values())
        for event in self._cancel.values():
            event.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
