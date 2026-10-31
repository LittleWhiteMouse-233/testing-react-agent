from __future__ import annotations

from app.api import repository as api_repository

import asyncio
from contextlib import nullcontext
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.tools import StructuredTool

from app.container import Container
from app.execution.run_service import RunConflict
from app.domain.errors import describe_exception
from app.domain.execution import TestRunVerdict as RunVerdict
from app.domain.resources import ResourceCatalogSnapshot
from app.resources.mcp import MCPConnectionError
from mcp_support import build_client, create_plan, finish_turn, tool_turn


@pytest.mark.parametrize("blocked_stage", ["plan", "tools"])
def test_competing_starts_are_rejected_during_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_stage: str,
) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        plan = create_plan(client)
        other_plan = create_plan(client)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        original_get_plan = container.repository.get_test_plan
        original_discover = container.tools.discover_tools

        async def exercise() -> None:
            entered, release = asyncio.Event(), asyncio.Event()
            queried_plan_ids: list[str] = []
            preparation_calls = 0

            async def get_test_plan(test_plan_id: str):
                queried_plan_ids.append(test_plan_id)
                if blocked_stage == "plan":
                    entered.set()
                    await release.wait()
                return await original_get_plan(test_plan_id)

            async def discover_tools():
                nonlocal preparation_calls
                preparation_calls += 1
                if blocked_stage == "tools":
                    entered.set()
                    await release.wait()
                return await original_discover()

            monkeypatch.setattr(container.repository, "get_test_plan", get_test_plan)
            monkeypatch.setattr(container.tools, "discover_tools", discover_tools)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                startup = asyncio.create_task(api.post("/api/runs", json={
                    "test_plan_id": plan["id"], "assumptions_confirmed": True,
                }))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    for competing_plan in (plan, other_plan):
                        response = await asyncio.wait_for(api.post("/api/runs", json={
                            "test_plan_id": competing_plan["id"], "assumptions_confirmed": True,
                        }), 1)
                        assert response.status_code == 409, response.text
                        assert response.json() == {
                            "code": "active_run_exists", "message": "Run preparation is already in progress",
                        }
                    assert queried_plan_ids == [plan["id"]]
                    assert preparation_calls == (1 if blocked_stage == "tools" else 0)
                    assert (await api.get("/api/runs")).json()["total"] == 0
                finally:
                    startup.cancel()
                    await asyncio.gather(startup, return_exceptions=True)
                assert container.run_service._work is None
                monkeypatch.setattr(container.repository, "get_test_plan", original_get_plan)
                monkeypatch.setattr(container.tools, "discover_tools", original_discover)
                run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
                await container.run_service.cancel(run.id)
                await container.run_service.shutdown()

        assert client.portal is not None
        client.portal.call(exercise)


@pytest.mark.parametrize("rejection", ["missing_plan", "case_busy"])
def test_rejected_start_releases_run_entry(
    tmp_path: Path, rejection: str,
) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        plan = create_plan(client)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                with (nullcontext() if rejection == "missing_plan"
                      else container.test_case_lock.hold(plan["test_case_id"], "editing")):
                    response = await api.post("/api/runs", json={
                        "test_plan_id": str(uuid4()) if rejection == "missing_plan" else plan["id"],
                        "assumptions_confirmed": True,
                    })
                assert response.status_code == (404 if rejection == "missing_plan" else 409)
                assert response.json()["code"] == ("run_input_not_found" if rejection == "missing_plan" else "test_case_busy")
                assert container.run_service._work is None
                assert (await api.get("/api/runs")).json()["total"] == 0
                run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
                await container.run_service.cancel(run.id)
                await container.run_service.shutdown()

        assert client.portal is not None
        client.portal.call(exercise)


@pytest.mark.parametrize("interruption", ["request", "shutdown"])
def test_interrupted_plan_lookup_releases_run_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_get_plan = container.repository.get_test_plan

        async def exercise() -> None:
            entered, cleaned = asyncio.Event(), asyncio.Event()

            async def get_test_plan(test_plan_id: str):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    return await original_get_plan(test_plan_id)
                finally:
                    cleaned.set()

            monkeypatch.setattr(container.repository, "get_test_plan", get_test_plan)
            startup = asyncio.create_task(container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                if interruption == "shutdown":
                    await asyncio.wait_for(container.run_service.shutdown(), 5)
                    assert cleaned.is_set()
                else:
                    startup.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(startup, 5)
                assert cleaned.is_set()
                assert container.run_service._work is None
                monkeypatch.setattr(container.repository, "get_test_plan", original_get_plan)
                if interruption == "shutdown":
                    with pytest.raises(RuntimeError, match="shutting down"):
                        await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
                else:
                    run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
                    await container.run_service.cancel(run.id)
                    await container.run_service.shutdown()
            finally:
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)

        assert client.portal is not None
        client.portal.call(exercise)


def test_terminal_persistence_keeps_run_and_case_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        plan = create_plan(client)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        original_finish = container.repository.finish_run

        async def exercise() -> None:
            finishing, release = asyncio.Event(), asyncio.Event()

            async def finish_run(test_run_id: str, verdict: RunVerdict) -> None:
                finishing.set()
                await release.wait()
                await original_finish(test_run_id, verdict)

            monkeypatch.setattr(container.repository, "finish_run", finish_run)
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            work = container.run_service._work
            assert work is not None and work.task is not None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                try:
                    await asyncio.wait_for(finishing.wait(), 10)
                    response = await asyncio.wait_for(api.post("/api/runs", json={
                        "test_plan_id": plan["id"], "assumptions_confirmed": True,
                    }), 1)
                    assert response.status_code == 409, response.text
                    assert response.json() == {
                        "code": "active_run_exists", "message": f"Run {run.id} is already active",
                    }
                    archive = await api.post(f"/api/test-cases/{plan['test_case_id']}/archive")
                    assert archive.status_code == 409
                    assert archive.json()["code"] == "test_case_has_active_run"
                finally:
                    release.set()
                    await asyncio.wait_for(work.task, 10)
                assert container.run_service._work is None
                assert (await api.post(f"/api/test-cases/{plan['test_case_id']}/archive")).status_code == 204

        assert client.portal is not None
        client.portal.call(exercise)


@pytest.mark.parametrize("blocked_stage", ["configuration", "tools", "resources"])
def test_one_preparation_timeout_covers_every_stage_and_releases_run_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_stage: str,
) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        stages: list[str] = []
        should_block = True

        async def stage(name: str) -> None:
            stages.append(name)
            if should_block and name == blocked_stage:
                await asyncio.Event().wait()

        async def load_clients() -> None:
            await stage("configuration")

        async def discover_tools():
            await stage("tools")
            return container.tools.additional_tools

        async def discover_resources() -> ResourceCatalogSnapshot:
            await stage("resources")
            return ResourceCatalogSnapshot()

        async def exercise():
            nonlocal should_block
            monkeypatch.setattr(container.mcp_clients, "load_clients", load_clients)
            monkeypatch.setattr(container.tools, "discover_tools", discover_tools)
            monkeypatch.setattr(container.skills, "discover_resources", discover_resources)
            container.run_service.discovery_timeout_seconds = 0.05
            with pytest.raises(MCPConnectionError, match="TimeoutError") as captured:
                await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            expected_phases = {"configuration": "MCP configuration loading", "tools": "MCP tool discovery",
                               "resources": "Skill and resource discovery"}
            assert str(captured.value).startswith(expected_phases[blocked_stage])
            assert stages == ["configuration", "tools", "resources"][:["configuration", "tools", "resources"].index(blocked_stage) + 1]
            await asyncio.sleep(0)
            assert container.run_service._work is None
            should_block = False
            stages.clear()
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            assert stages == ["configuration", "tools", "resources"]
            await container.run_service.shutdown()
            assert (await container.repository.get_test_run(run.id)).status.value == "finished"

        assert client.portal is not None
        client.portal.call(exercise)


@pytest.mark.parametrize("invalid_config", [True, False])
def test_preparation_errors_are_converted_once_at_the_public_boundary(tmp_path: Path, invalid_config: bool) -> None:
    import json

    with build_client(tmp_path) as client:
        plan = create_plan(client)
        config = tmp_path / "mcp.json"
        config.write_text("not json" if invalid_config else json.dumps({
            "mcpServers": {"missing": {"command": "nonexistent-mcp-executable"}},
        }), encoding="utf-8")
        response = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert response.status_code == 502
        assert response.json()["code"] == "mcp_unavailable"
        diagnostic = response.json()["message"]
        assert diagnostic.startswith("MCP configuration loading" if invalid_config else "MCP tool discovery")
        assert "MCPConnectionError" not in diagnostic


@pytest.mark.parametrize("cancelled", [False, True])
def test_tool_failure_blocks_releases_slot_and_allows_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    with build_client(tmp_path, turns=[tool_turn(1)]) as client:
        plan = create_plan(client, task_count=2)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_discover = container.tools.discover_tools

        async def fail_tool() -> str:
            if cancelled:
                assert container.run_service._work is not None
                container.run_service._work.cancellation.set()
            raise OSError("tool invocation mechanism failed")

        async def discover_tools():
            return (StructuredTool.from_function(
                coroutine=fail_tool, name="fixture_inspect", description="Observe target",
                metadata={"tool_source": "fixture"},
            ), *container.tools.additional_tools)

        async def exercise():
            monkeypatch.setattr(container.tools, "discover_tools", discover_tools)
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            work = container.run_service._work
            assert work is not None and work.task is not None
            await asyncio.wait_for(work.task, 10)
            await asyncio.sleep(0)
            detail = await container.repository.get_test_run_detail(run.id)
            assert detail.run.verdict is not None and detail.run.verdict.value == "BLOCKED"
            assert [task.status.value for task in detail.task_runs] == ["blocked", "skipped"]
            events = await api_repository.list_events(container.sessions, run.id)
            assert sum(entry.event.type == "execution.error" for entry in events) == 1
            assert sum(entry.event.type == "run.finished" for entry in events) == 1
            assert container.run_service._work is None
            monkeypatch.setattr(container.tools, "discover_tools", original_discover)
            next_run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            assert next_run.id != run.id
            assert await container.run_service.cancel(next_run.id)
            await container.run_service.shutdown()

        assert client.portal is not None
        client.portal.call(exercise)


def test_single_run_slot_and_shutdown_wait_for_active_call(tmp_path: Path) -> None:
    from mcp_support import read_records, replay_call

    with build_client(tmp_path, calls=[replay_call(delay=0.5)], turns=[tool_turn(1), tool_turn(2)]) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise():
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            async def wait_for_active_call() -> None:
                while not any(record["phase"] == "started" for record in read_records(tmp_path)):
                    await asyncio.sleep(0.01)

            # Process startup is not the behavior under test. Synchronize on
            # actual execution before asserting the shutdown boundary.
            await asyncio.wait_for(wait_for_active_call(), 15)
            with pytest.raises(RunConflict):
                await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            shutdown = asyncio.create_task(container.run_service.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            with pytest.raises(RuntimeError, match="shutting down"):
                await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            await asyncio.wait_for(shutdown, 10)
            finished = await container.repository.get_test_run(run.id)
            assert finished.verdict is not None and finished.verdict.value == "CANCELLED"
            assert container.run_service._work is None

        assert client.portal is not None
        client.portal.call(exercise)
    records = read_records(tmp_path)
    assert sum(record["phase"] == "started" for record in records) == 1
    assert sum(record["phase"] == "completed" for record in records) == 1


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
            assert (await container.repository.get_test_run(run.id)).status.value != "finished"
            events = await api_repository.list_events(container.sessions, run.id)
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
            events = await api_repository.list_events(container.sessions, cancelled.id)
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


def test_shutdown_cancels_discovery_before_run_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise():
            preparing = asyncio.Event()
            cleaned = asyncio.Event()

            async def discover_tools():
                try:
                    preparing.set()
                    await asyncio.Event().wait()
                    return container.tools.additional_tools
                finally:
                    cleaned.set()

            monkeypatch.setattr(container.tools, 'discover_tools', discover_tools)
            startup = asyncio.create_task(container.run_service.start(test_plan_id=plan['id'], assumptions_confirmed=True))
            await preparing.wait()
            await container.run_service.shutdown()
            with pytest.raises(asyncio.CancelledError):
                await startup
            assert cleaned.is_set()

        assert client.portal is not None
        client.portal.call(exercise)
