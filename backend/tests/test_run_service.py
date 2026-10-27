from __future__ import annotations

from app.api import repository as api_repository

import asyncio
from pathlib import Path
from typing import cast

import pytest
from langchain_core.tools import StructuredTool

from app.container import Container
from app.execution.run_service import RunConflict
from app.domain.errors import describe_exception
from app.domain.resources import ResourceCatalogSnapshot
from app.resources.mcp import MCPConnectionError
from mcp_support import build_client, create_plan, finish_turn, tool_turn


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
