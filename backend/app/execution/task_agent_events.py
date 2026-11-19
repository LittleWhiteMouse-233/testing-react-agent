from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from app.domain.execution import NodeExecutionEvent
from app.domain.ids import TaskRunId, TestRunId
from app.persistence.run_repository import SqlAlchemyRunRepository


class TaskNodeTiming(AsyncCallbackHandler):
    """Collect direct graph children only. Nested chains share node metadata."""
    raise_error = True
    run_inline = True

    def __init__(self, repository: SqlAlchemyRunRepository, test_run_id: TestRunId, task_run_id: TaskRunId) -> None:
        self.repository = repository
        self.test_run_id = test_run_id
        self.task_run_id = task_run_id
        self.graph_run_id: UUID | None = None
        self.starts: dict[UUID, tuple[str, datetime]] = {}

    async def on_chain_start(self, serialized: dict[str, Any] | None, inputs: Any, *,
                             run_id: UUID, parent_run_id: UUID | None = None,
                             metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if self.graph_run_id is None:
            self.graph_run_id = run_id
        elif parent_run_id == self.graph_run_id and metadata and (node := metadata.get("langgraph_node")):
            self.starts[run_id] = (node, datetime.now(UTC))

    async def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        await self._finish(run_id)

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        await self._finish(run_id)

    async def _finish(self, run_id: UUID) -> None:
        started = self.starts.pop(run_id, None)
        if started is not None:
            node_name, started_at = started
            await self.repository.append_event(NodeExecutionEvent(
                test_run_id=self.test_run_id, task_run_id=self.task_run_id,
                node_name=node_name, started_at=started_at, finished_at=datetime.now(UTC),
            ))
