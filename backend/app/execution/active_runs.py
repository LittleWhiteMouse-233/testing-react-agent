from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class ActiveRunHandle:
    """同一次活跃 TestRun 必须共同注册和移除的任务与取消信号。"""

    execution_task: asyncio.Task[None]
    cancellation_event: asyncio.Event


class ActiveRunRegistry:
    """进程内 TestRun 生命周期索引；只有 RunService 和 shutdown 持有。"""

    def __init__(self) -> None:
        self._active_runs: dict[str, ActiveRunHandle] = {}

    def register(
        self,
        test_run_id: str,
        execution_task: asyncio.Task[None],
        cancellation_event: asyncio.Event,
    ) -> None:
        if test_run_id in self._active_runs:
            raise ValueError(f"Test run is already registered: {test_run_id}")
        self._active_runs[test_run_id] = ActiveRunHandle(
            execution_task=execution_task,
            cancellation_event=cancellation_event,
        )

    def cancel(self, test_run_id: str) -> bool:
        handle = self._active_runs.get(test_run_id)
        if handle is None:
            return False
        handle.cancellation_event.set()
        return True

    def unregister(self, test_run_id: str) -> None:
        self._active_runs.pop(test_run_id, None)

    @property
    def active_run_id(self) -> str | None:
        """Keep the run slot occupied until its MCP sessions have closed."""
        return next(iter(self._active_runs), None)

    async def shutdown(self) -> None:
        handles = tuple(self._active_runs.values())
        for handle in handles:
            handle.cancellation_event.set()
        if handles:
            await asyncio.gather(
                *(handle.execution_task for handle in handles),
                return_exceptions=True,
            )
