from __future__ import annotations

import asyncio

import pytest

from app.execution.active_runs import ActiveRunRegistry


@pytest.mark.asyncio
async def test_active_run_registry_owns_one_atomic_handle_and_missing_cancel_is_read_only() -> None:
    active_runs = ActiveRunRegistry()
    assert active_runs.cancel("missing") is False

    cancellation_event = asyncio.Event()

    async def execution() -> None:
        await cancellation_event.wait()

    execution_task = asyncio.create_task(execution())
    active_runs.register("run-1", execution_task, cancellation_event)
    with pytest.raises(ValueError, match="already registered"):
        active_runs.register("run-1", execution_task, cancellation_event)

    await active_runs.shutdown()
    assert cancellation_event.is_set()
    assert execution_task.done()
    active_runs.unregister("run-1")
    assert active_runs.cancel("run-1") is False
