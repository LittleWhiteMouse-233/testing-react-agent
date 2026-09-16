"""UI read contracts: source-backed counts, pagination and Shanghai day boundaries."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import repository as api_repository
from app.api.schemas import TestRunFilterQuery as RunFilterQuery, TestRunPageQuery as RunPageQuery
from app.artifacts import ArtifactStore
from app.container import Container
from app.domain.execution import ReasonCode, TaskRunResult, TaskRunStatus, TestRunStatus as RunStatus, TestRunVerdict as RunVerdict
from app.domain.planning import TestPlanOrigin as PlanOrigin
from app.event_stream import EventBus, EventWriter
from app.persistence.db import build_engine, build_session_factory, init_database
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.persistence.models import TestRunRow as RunRow
from mcp_support import build_client, create_plan, read_records, start_run, wait_for_run
from test_structure import plan_draft, planning_context, run_snapshot


def test_history_queries_join_existing_facts_without_writes(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client, task_count=2)
        run_id = start_run(client, plan)
        wait_for_run(client, run_id)
        report = client.get(f"/api/runs/{run_id}").json()
        client.post(f"/api/runs/{run_id}/exports", json={"format": "json"})
        # A new plan must not change the historical run's definition count.
        client.post(f"/api/test-plans/{plan['id']}/revisions", json={"content": {
            **plan["content"], "tasks": [plan["content"]["tasks"][0]["definition"]],
        }})
        container = cast(Container, cast(FastAPI, client.app).state.container)
        writes: list[str] = []

        def observe_sql(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(statement)

        event.listen(container.engine.sync_engine, "before_cursor_execute", observe_sql)
        mcp_before = read_records(tmp_path)
        cases = client.get("/api/test-cases", params={"search": "Protocol"}).json()
        assert cases["items"][0]["latest_plan_version"] == plan["version_number"] + 1
        listed = client.get("/api/runs", params={"test_case_id": plan["test_case_id"]}).json()
        assert listed["total"] == 1
        run = listed["items"][0]
        assert run["id"] == run_id
        assert run["test_case_name"] == plan["planning_context"]["test_case_content"]["name"]
        assert run["task_count"] == 2
        assert run["task_status_counts"] == {"passed": 2}
        assert run["screenshot_count"] == len(report["artifacts"]) == 2
        statistics = client.get("/api/runs/statistics").json()
        assert statistics["run_count"] == 1
        assert statistics["verdict_counts"] == {"PASS": 1, "FAIL": 0, "BLOCKED": 0, "CANCELLED": 0}
        assert statistics["average_duration_seconds"] is not None
        assert client.get("/api/runs", params={"search": run_id[:12]}).json()["total"] == 1
        assert client.get(f"/api/runs/{run_id}/events").status_code == 404
        assert client.get(f"/api/runs/{run_id}/report").status_code == 404
        assert read_records(tmp_path) == mcp_before
        assert writes == []


def test_run_filter_statistics_are_unpaginated_and_use_half_open_utc_range(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        original_id = start_run(client, plan)
        wait_for_run(client, original_id)
        container = cast(Container, cast(FastAPI, client.app).state.container)
        midnight = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
        ids = [str(uuid4()) for _ in range(5)]

        async def seed_history() -> None:
            async with container.sessions() as session:
                original = await session.get(RunRow, original_id)
                assert original is not None
                original.created_at = midnight - timedelta(seconds=1)
                for index, verdict in enumerate([RunVerdict.PASS, RunVerdict.FAIL, RunVerdict.CANCELLED, RunVerdict.BLOCKED, None]):
                    created = midnight + timedelta(hours=index)
                    started = None if index >= 3 else created
                    session.add(RunRow(
                        id=ids[index], test_plan_id=plan["id"], snapshot_json=original.snapshot_json,
                        status=RunStatus.PENDING if verdict is None else RunStatus.FINISHED, verdict=verdict,
                        created_at=created, started_at=started,
                        finished_at=None if verdict is None else created + timedelta(seconds=30 * (index + 1)),
                    ))
                await session.commit()

        assert client.portal is not None
        client.portal.call(seed_history)
        filters = {"created_from": "2026-09-14T00:00:00+08:00", "created_before": "2026-09-15T00:00:00+08:00"}
        first = client.get("/api/runs", params={**filters, "limit": 2}).json()
        second = client.get("/api/runs", params={**filters, "limit": 2, "offset": 2}).json()
        assert first["total"] == second["total"] == 5
        assert [run["id"] for run in first["items"] + second["items"]] == list(reversed(ids))[:4]
        statistics = client.get("/api/runs/statistics", params=filters).json()
        assert statistics["run_count"] == 5
        assert statistics["verdict_counts"] == {"PASS": 1, "FAIL": 1, "BLOCKED": 1, "CANCELLED": 1}
        assert statistics["average_duration_seconds"] == pytest.approx(60, abs=0.001)
        assert statistics["daily"][0]["date"] == "2026-09-14"
        assert statistics["daily"][0]["total_duration_seconds"] == pytest.approx(180, abs=0.001)
        assert client.get("/api/runs", params={**filters, "verdict": "FAIL"}).json()["total"] == 1
        assert client.get("/api/runs/statistics", params={**filters, "verdict": "FAIL"}).json()["run_count"] == 1
        active = client.get("/api/runs", params=[("status", "pending"), ("status", "running")]).json()
        assert [run["id"] for run in active["items"]] == [ids[-1]]
        exclusive_end = client.get("/api/runs", params={**filters, "created_before": "2026-09-14T01:00:00+08:00"}).json()
        assert [run["id"] for run in exclusive_end["items"]] == [ids[0]]
        empty = client.get("/api/runs/statistics", params={"search": "missing"}).json()
        assert empty["run_count"] == 0 and empty["daily"] == [] and empty["average_duration_seconds"] is None
        assert client.get("/api/runs", params={"created_from": "2026-09-14"}).status_code == 422
        assert client.get("/api/runs/statistics", params={**filters, "created_before": filters["created_from"]}).status_code == 422


def test_case_search_is_literal_and_paginates_all_matches(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        for index in range(3):
            client.post("/api/test-cases", json={"name": f"Wi-Fi_{index}%", "source_text": "检查网络"})
        client.post("/api/test-cases", json={"name": "HDMI", "source_text": "输入信号"})
        first = client.get("/api/test-cases", params={"search": "%", "limit": 2}).json()
        second = client.get("/api/test-cases", params={"search": "%", "limit": 2, "offset": 2}).json()
        assert first["total"] == second["total"] == 3
        assert len({case["id"] for case in first["items"] + second["items"]}) == 3
        assert all(case["latest_plan_version"] is None for case in first["items"])
        assert client.get("/api/test-cases", params={"search": "输入信号"}).json()["total"] == 1


@pytest.mark.parametrize("response_kind", ["report", "detail", "list", "statistics"])
async def test_response_keeps_one_snapshot_during_committed_run_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response_kind: str,
) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'snapshot.db').as_posix()}")
    await init_database(engine)
    sessions = build_session_factory(engine)
    repository = SqlAlchemyRunRepository(sessions, EventWriter(sessions, EventBus()))
    case = await api_repository.create_test_case(sessions, planning_context().test_case_content)
    plan = await repository.create_plan(test_case_id=case.id, draft=plan_draft(),
        planning_context=planning_context(), origin=PlanOrigin.PLANNING)
    run = await repository.create_test_run(test_plan_id=plan.id, snapshot=run_snapshot())
    changed = False

    async def commit_changes() -> None:
        nonlocal changed
        if changed:
            return
        changed = True
        # Real independent writer sessions commit while the reader is open.
        await repository.start_run(run.id)
        task = await repository.start_task(run.id, plan.content.tasks[0])
        await ArtifactStore(tmp_path / "artifacts", sessions).save_screenshot(
            task_run_id=task.id, content=b"screenshot", mime_type="image/png")
        await repository.finish_task(task.id, status=TaskRunStatus.CANCELLED, cycle_count=0,
            result=TaskRunResult(reason_code=ReasonCode.USER_CANCELLED, summary="Cancelled", evidence_artifact_ids=[]))
        await repository.finish_run(run.id, RunVerdict.CANCELLED)

    original_task_rows = SqlAlchemyRunRepository._task_rows
    original_execute = AsyncSession.execute

    async def task_rows_with_writer(session, plan_id):
        rows = await original_task_rows(session, plan_id)
        await commit_changes()
        return rows

    async def execute_with_writer(session, statement, *args, **kwargs):
        result = await original_execute(session, statement, *args, **kwargs)
        if str(statement).lstrip().upper().startswith("SELECT"):
            await commit_changes()
        return result

    try:
        if response_kind in {"report", "detail"}:
            monkeypatch.setattr(SqlAlchemyRunRepository, "_task_rows", staticmethod(task_rows_with_writer))
        else:
            monkeypatch.setattr(AsyncSession, "execute", execute_with_writer)
        if response_kind == "report":
            report = await api_repository.read_test_run_report(sessions, run.id)
            assert report.detail.run.status == RunStatus.PENDING
            assert report.detail.task_runs == report.events == report.artifacts == []
        elif response_kind == "detail":
            detail = await repository.get_test_run_detail(run.id)
            assert detail.run.status == RunStatus.PENDING and detail.task_runs == []
        elif response_kind == "list":
            page = await api_repository.list_test_runs(sessions, RunPageQuery())
            assert page.total == 1
            assert page.items[0].status == RunStatus.PENDING
            assert page.items[0].task_status_counts == {} and page.items[0].screenshot_count == 0
        else:
            statistics = await api_repository.test_run_statistics(sessions, RunFilterQuery())
            assert statistics.run_count == 1
            assert sum(statistics.verdict_counts.values()) == 0
            assert statistics.average_duration_seconds is None
            assert statistics.daily[0].average_duration_seconds is None
        assert changed
        current = await api_repository.read_test_run_report(sessions, run.id)
        assert current.detail.run.verdict == RunVerdict.CANCELLED
        assert len(current.artifacts) == len(current.detail.task_runs) == 1
        assert current.events[-1].event.type == "run.cancelled"
    finally:
        await engine.dispose()


def test_run_list_omits_snapshot_and_skips_empty_page_aggregates(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        run_id = start_run(client, plan)
        wait_for_run(client, run_id)
        container = cast(Container, cast(FastAPI, client.app).state.container)
        statements: list[str] = []

        def observe_sql(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(container.engine.sync_engine, "before_cursor_execute", observe_sql)
        try:
            page = client.get("/api/runs").json()
            assert page["items"][0]["id"] == run_id
            assert len(statements) == 5
            assert "test_runs.snapshot_json" not in statements[1]
            statements.clear()
            assert client.get("/api/runs", params={"offset": 100}).json() == {"items": [], "total": 1}
            assert len(statements) == 2
            statements.clear()
            assert client.get("/api/runs", params={"status": "pending"}).json() == {"items": [], "total": 0}
            assert len(statements) == 2
        finally:
            event.remove(container.engine.sync_engine, "before_cursor_execute", observe_sql)


def test_plan_versions_batch_tasks_and_preserve_history(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client, task_count=2)
        expected = client.get(f"/api/test-cases/{plan['test_case_id']}/plans").json()["items"]
        for index in range(3):
            content = {**plan["content"], "title": f"Revision {index}",
                       "tasks": [task["definition"] for task in reversed(plan["content"]["tasks"])]}
            plan = client.post(f"/api/test-plans/{plan['id']}/revisions", json={"content": content}).json()
            expected.insert(0, plan)
        container = cast(Container, cast(FastAPI, client.app).state.container)
        statements: list[str] = []

        def observe_sql(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(container.engine.sync_engine, "before_cursor_execute", observe_sql)
        try:
            page = client.get(f"/api/test-cases/{plan['test_case_id']}/plans").json()
            assert page == {"items": expected, "total": 5}
            assert len(statements) == 3
        finally:
            event.remove(container.engine.sync_engine, "before_cursor_execute", observe_sql)
        assert client.get(f"/api/test-cases/{uuid4()}/plans").status_code == 404
