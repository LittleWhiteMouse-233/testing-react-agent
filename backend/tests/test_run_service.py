from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from langchain_core.tools import StructuredTool

from app.container import Container
from app.execution.run_service import RunConflict, RunResourcesUnavailable
from app.domain.errors import describe_exception
from mcp_support import build_client, create_plan, finish_turn, tool_turn


@pytest.mark.parametrize("failure_kind", ["none", "exception", "group"])
@pytest.mark.parametrize("task_status,verdict", [("passed", "PASS"), ("failed", "FAIL"), ("blocked", "BLOCKED")])
def test_run_terminal_precedes_cleanup_and_cleanup_failure_only_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    failure_kind: str, task_status: str, verdict: str,
) -> None:
    with build_client(tmp_path, turns=[tool_turn(1), finish_turn(2, task_status)]) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        cleaning, release = asyncio.Event(), asyncio.Event()
        original = container.tools.connect
        cleanup_failure = (
            ExceptionGroup("closing sessions", [OSError("first close failed"), RuntimeError("second close failed")])
            if failure_kind == "group" else OSError("first close failed")
        )

        @asynccontextmanager
        async def connect(**kwargs):
            async with original(**kwargs) as tools:
                yield tools
            cleaning.set()
            await asyncio.wait_for(release.wait(), 15)
            if failure_kind != "none":
                # Match the resource provider contract: report unsafe cleanup
                # through its callback before propagating the failure.
                kwargs["on_cleanup_failure"](cleanup_failure)
                raise cleanup_failure

        async def start_and_wait_for_cleanup():
            monkeypatch.setattr(container.tools, "connect", connect)
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            work = container.run_service._work
            assert work is not None and work.task is not None
            await asyncio.wait_for(cleaning.wait(), 10)
            finished = await container.repository.get_test_run(run.id)
            assert finished.status.value == "finished"
            assert finished.verdict is not None and finished.verdict.value == verdict
            assert not work.task.done()
            with pytest.raises(RunConflict):
                await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            assert await container.run_service.cancel(run.id) is False
            return run.id

        async def finish_cleanup():
            work = container.run_service._work
            assert work is not None and work.task is not None
            release.set()
            if failure_kind == "none":
                await asyncio.wait_for(work.task, 5)
            else:
                with pytest.raises(type(cleanup_failure)) as captured:
                    await asyncio.wait_for(work.task, 5)
                assert captured.value is cleanup_failure
            await asyncio.sleep(0)
            assert (container.run_service._work is None) == (failure_kind == "none")

        assert client.portal is not None
        run_id = client.portal.call(start_and_wait_for_cleanup)
        # SSE and exports must finish while resource cleanup is still blocked.
        history = client.get(f"/api/runs/{run_id}/events").json()["items"]
        errors = [entry["event"] for entry in history if entry["event"]["type"] == "execution.error"]
        assert not errors
        terminals = [entry for entry in history if entry["event"]["type"] in {"run.finished", "run.cancelled"}]
        assert terminals == [history[-1]]
        stream = client.get(f"/api/runs/{run_id}/stream")
        streamed = [json.loads(line.removeprefix("data: ")) for line in stream.text.splitlines() if line.startswith("data: ")]
        assert streamed == history
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["events"] == history
        exported = client.post(f"/api/runs/{run_id}/exports", json={"format": "json"}).json()
        assert client.get(f"/api/artifacts/{exported['id']}").json() == report
        report_after_export = client.get(f"/api/runs/{run_id}/report").json()
        client.portal.call(finish_cleanup)
        assert client.get(f"/api/runs/{run_id}/report").json() == report_after_export
        records = [record for record in caplog.records if record.name.startswith("app.") and record.levelno >= 40]
        assert len(records) == (0 if failure_kind == "none" else 1)
        if records:
            assert records[0].name == "app.execution.run_service"
            assert records[0].exc_info is not None and records[0].exc_info[1] is cleanup_failure
            description = describe_exception(cleanup_failure, phase="Cleanup failed")
            assert "first close failed" in description
            if failure_kind == "group":
                assert "second close failed" in description

        async def start_next_run():
            monkeypatch.setattr(container.tools, "connect", original)
            next_run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            assert next_run.id != run_id
            assert await container.run_service.cancel(next_run.id)
            await container.run_service.shutdown()

        if failure_kind == "none":
            client.portal.call(start_next_run)
        else:
            response = client.post('/api/runs', json={'test_plan_id': plan['id'], 'assumptions_confirmed': True})
            assert response.status_code == 409
            assert response.json()['code'] == 'run_resources_unavailable'


@pytest.mark.parametrize("report_stage", ["execution", "terminal"])
def test_unreportable_failure_is_observed_once_without_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, report_stage: str,
) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_failure = OSError("original operation failed")
        report_failure = OSError("error reporting unavailable")
        report_attempts = 0

        async def exercise():
            original_append = container.repository.append_event
            original_append_many = container.repository.append_events

            async def append_events(events):
                if report_stage == "execution" and any(
                    event.type == "message.appended" and event.message.role == "tool" for event, _ in events
                ):
                    raise original_failure
                return await original_append_many(events)

            async def append_event(event, **kwargs):
                nonlocal report_attempts
                if event.type == "execution.error":
                    report_attempts += 1
                    raise report_failure
                return await original_append(event, **kwargs)

            async def finish_run(*args, **kwargs):
                nonlocal report_attempts
                report_attempts += 1
                raise report_failure

            monkeypatch.setattr(container.repository, "append_events", append_events)
            monkeypatch.setattr(container.repository, "append_event", append_event)
            if report_stage == "terminal":
                monkeypatch.setattr(container.repository, "finish_run", finish_run)
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            work = container.run_service._work
            assert work is not None and work.task is not None
            with pytest.raises(OSError) as captured:
                await asyncio.wait_for(work.task, 10)
            assert captured.value is report_failure
            await asyncio.sleep(0)
            assert container.run_service._work is None
            assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("mcp-session-")]
            assert (await container.repository.get_test_run(run.id)).status.value != "finished"
            events = await container.repository.list_events(run.id)
            assert not any(entry.event.type in {"run.finished", "run.cancelled"} for entry in events)
            if report_stage == "terminal":
                assert not any(entry.event.type == "execution.error" for entry in events)

        assert client.portal is not None
        client.portal.call(exercise)

        records = [record for record in caplog.records if record.name.startswith("app.") and record.levelno >= 40]
        assert len(records) == 1 and records[0].name == "app.execution.run_service"
        assert records[0].exc_info is not None and records[0].exc_info[1] is report_failure
        assert report_attempts == 1
        if report_stage != "terminal":
            assert "original operation failed" in describe_exception(report_failure, phase="Reporting failed")


@pytest.mark.parametrize("trigger", ["cancel", "timeout"])
@pytest.mark.parametrize("cleanup_kind", ["failure", "timeout"])
def test_tool_cleanup_fault_blocks_and_locks_run_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str, cleanup_kind: str,
) -> None:
    with build_client(tmp_path, turns=[tool_turn(1)], tool_call_timeout_seconds=0.1,
                      tool_cleanup_timeout_seconds=0.05) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise():
            started, release = asyncio.Event(), asyncio.Event()
            invocation_tasks: list[asyncio.Task] = []
            close_count = 0

            async def inspect() -> str:
                nonlocal close_count
                current = asyncio.current_task()
                assert current is not None
                invocation_tasks.append(current)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    close_count += 1
                    if cleanup_kind == "timeout":
                        await release.wait()
                    else:
                        raise OSError("close failed")
                return "late result"

            tool = StructuredTool.from_function(
                name="fixture_inspect", coroutine=inspect, description="Observe fixture",
                metadata={"mcp_server_name": "fixture"},
            )

            @asynccontextmanager
            async def connect(**kwargs):
                yield (tool,)

            monkeypatch.setattr(container.tools, "connect", connect)
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            work = container.run_service._work
            assert work is not None and work.task is not None
            await asyncio.wait_for(started.wait(), 2)
            if trigger == "cancel":
                assert await container.run_service.cancel(run.id)
            await asyncio.wait_for(asyncio.shield(work.task), 2)
            finished = await container.repository.get_test_run_detail(run.id)
            assert finished.run.verdict is not None and finished.run.verdict.value == "BLOCKED"
            assert finished.task_runs[0].result is not None
            assert finished.task_runs[0].result.reason_code.value == "tool_failed"
            events = await container.repository.list_events(run.id)
            assert sum(entry.event.type == "execution.error" for entry in events) == 1
            assert not any(entry.event.type == "message.appended" and entry.event.message.role == "tool"
                           for entry in events)
            assert work.cleanup_failure is not None and container.run_service._work is work
            with pytest.raises(RunResourcesUnavailable):
                await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            release.set()
            await asyncio.gather(*invocation_tasks, return_exceptions=True)
            with pytest.raises(RunResourcesUnavailable):
                await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            assert close_count == 1
            return run.id

        assert client.portal is not None
        client.portal.call(exercise)
        denied = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert denied.status_code == 409 and denied.json()["code"] == "run_resources_unavailable"


def test_startup_cleanup_failure_locks_entry_without_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        failure = OSError("startup cleanup failed")

        @asynccontextmanager
        async def connect(**kwargs):
            kwargs["on_cleanup_failure"](failure)
            from app.tools import MCPConnectionError
            raise MCPConnectionError("startup failed") from failure
            yield ()

        monkeypatch.setattr(container.tools, "connect", connect)
        payload = {"test_plan_id": plan["id"], "assumptions_confirmed": True}
        assert client.post("/api/runs", json=payload).status_code == 502
        denied = client.post("/api/runs", json=payload)
        assert denied.status_code == 409 and denied.json()["code"] == "run_resources_unavailable"
        assert container.run_service._work is not None
        assert container.run_service._work.test_run_id is None



def test_single_run_slot_covers_cleanup_and_shutdown_rejects_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise():
            cleaning = asyncio.Event()
            release = asyncio.Event()
            original = container.tools.connect

            @asynccontextmanager
            async def connect(**kwargs):
                async with original(**kwargs) as tools:
                    yield tools
                cleaning.set()
                await release.wait()

            monkeypatch.setattr(container.tools, 'connect', connect)
            first, second = await asyncio.gather(*[
                container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True)
                for _ in range(2)
            ], return_exceptions=True)
            assert not isinstance(first, BaseException)
            assert isinstance(second, RunConflict)
            await asyncio.wait_for(cleaning.wait(), 10)
            finished = await container.repository.get_test_run(first.id)
            assert finished.verdict is not None and finished.verdict.value == 'PASS'
            with pytest.raises(RunConflict):
                await container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True)
            shutdown = asyncio.create_task(container.run_service.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            release.set()
            await shutdown
            assert await container.repository.get_test_run(first.id) == finished
            with pytest.raises(RuntimeError, match='shutting down'):
                await container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True)

        assert client.portal is not None
        client.portal.call(exercise)


def test_cancelled_start_after_commit_finishes_run_and_allows_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise():
            committed = asyncio.Event()
            release = asyncio.Event()
            original = container.repository.create_test_run
            run_ids: list[str] = []

            async def create_run(**kwargs):
                run = await original(**kwargs)
                run_ids.append(run.id)
                committed.set()
                await release.wait()
                return run

            monkeypatch.setattr(container.repository, 'create_test_run', create_run)
            startup = asyncio.create_task(container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True))
            await asyncio.wait_for(committed.wait(), 10)
            startup.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await startup
            cancelled = await container.repository.get_test_run(run_ids[0])
            assert cancelled.status.value == 'finished'
            assert cancelled.verdict is not None and cancelled.verdict.value == 'CANCELLED'
            events = await container.repository.list_events(cancelled.id)
            terminals = [entry for entry in events if entry.event.type in {'run.finished', 'run.cancelled'}]
            assert terminals == [events[-1]] and terminals[0].event.type == 'run.cancelled'
            assert not any(entry.event.type == 'execution.error' for entry in events)
            next_run = await container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True)
            assert next_run.id != cancelled.id
            assert await container.run_service.cancel(cancelled.id) is False
            assert await container.run_service.cancel(next_run.id) is True
            await container.run_service.shutdown()
            finished = await container.repository.get_test_run(next_run.id)
            assert finished.verdict is not None and finished.verdict.value == 'CANCELLED'

        assert client.portal is not None
        client.portal.call(exercise)


def test_shutdown_cleans_preparation_before_run_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise():
            preparing = asyncio.Event()
            cleaned = asyncio.Event()

            @asynccontextmanager
            async def connect(**kwargs):
                try:
                    preparing.set()
                    await asyncio.Event().wait()
                    yield ()
                finally:
                    cleaned.set()

            monkeypatch.setattr(container.tools, 'connect', connect)
            startup = asyncio.create_task(container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True))
            await preparing.wait()
            await container.run_service.shutdown()
            with pytest.raises(asyncio.CancelledError):
                await startup
            assert cleaned.is_set()

        assert client.portal is not None
        client.portal.call(exercise)
