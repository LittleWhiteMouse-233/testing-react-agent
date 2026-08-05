from __future__ import annotations

from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.domain.events import ExecutionEvent
from app.domain.execution import (
    OverallResult,
    RunSnapshot,
    TaskExecution,
    TaskOutcome,
)
from app.domain.planning import Task
from app.domain.tools import DeviceHealth, DeviceCapabilities


class ExecutionRepository(Protocol):
    async def load_snapshot(self, run_id: str) -> RunSnapshot: ...

    async def get_run_device_id(self, run_id: str) -> str: ...

    async def start_run(self, run_id: str, event: ExecutionEvent) -> None: ...

    async def start_task(
        self,
        run_id: str,
        execution: TaskExecution,
        event: ExecutionEvent,
    ) -> TaskExecution: ...

    async def update_cycle(
        self, task_run_id: str, cycle_count: int, event: ExecutionEvent
    ) -> None: ...

    async def finish_task(
        self, task_run_id: str, outcome: TaskOutcome, event: ExecutionEvent
    ) -> None: ...

    async def skip_remaining(
        self,
        run_id: str,
        executions: list[TaskExecution],
        event: ExecutionEvent,
    ) -> None: ...

    async def list_task_executions(self, run_id: str) -> list[TaskExecution]: ...

    async def finish_run(
        self,
        run_id: str,
        result: OverallResult,
        event: ExecutionEvent,
        *,
        cancelled: bool = False,
    ) -> None: ...

    async def recent_context(
        self, run_id: str, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    async def reconcile_orphaned(self) -> list[str]: ...


class ExecutionJournal(Protocol):
    async def append(
        self, event: ExecutionEvent, *, dedup_key: str | None = None
    ) -> dict[str, Any] | None: ...


class ArtifactRepository(Protocol):
    async def save_observation(
        self,
        *,
        run_id: str,
        task_run_id: str,
        content: bytes,
        mime_type: str,
        activity: str | None,
        cycle_count: int,
    ) -> Any: ...

    async def load_content(self, artifact_id: str) -> tuple[bytes, str]: ...


class DeviceControllerPort(Protocol):
    async def health(self) -> DeviceHealth: ...
    async def capabilities(self) -> DeviceCapabilities: ...


class CancellationRegistry(Protocol):
    def cancellation(self, run_id: str) -> Any: ...


class CompiledTaskAgent(Protocol):
    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]: ...


class CompiledTaskAgentFactory(Protocol):
    async def build(
        self,
        *,
        run_id: str,
        task_run_id: str,
        device_id: str,
        task: Task,
        capabilities: DeviceCapabilities,
        enabled_tool_names: set[str],
        cross_task_context: list[dict[str, Any]],
        checkpointer: BaseCheckpointSaver[str] | None,
    ) -> CompiledTaskAgent: ...
