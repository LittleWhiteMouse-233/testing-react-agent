"""Database operations for API consumers and report fact assembly.

These joins and aggregates serve browser lists, not execution decisions. Public
SQLAlchemy SELECT operations fill the API gap without changing repository writes
or domain entities. Separate aggregate queries avoid multiplying counts through
task/artifact joins. No projection is persisted.
"""
from __future__ import annotations

from datetime import timezone
from uuid import uuid4

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.schemas import (
    PageResponse, TestCaseListResponse, TestRunDailyStatistics, TestRunFilterQuery,
    TestRunListResponse, TestRunPageQuery, TestRunStatisticsResponse,
)
from app.domain.execution import StoredRunEvent, ArtifactType, TaskRunStatus, TestRunStatus, TestRunVerdict
from app.domain.planning import TestCase, TestCaseContent, TestPlan
from app.persistence.db import read_snapshot
from app.persistence.run_repository import read_test_run_detail
from app.persistence.mappers import artifact_from_row, stored_event_from_row, test_case_from_row, test_plan_from_rows, test_run_from_row
from app.reporting import TestRunReport
from app.persistence.models import ArtifactRow, RunEventRow, TaskRunRow, TestCaseRow, TestPlanRow, TestRunRow, TestTaskRow


async def list_test_cases(
    sessions: async_sessionmaker[AsyncSession], *, search: str, limit: int, offset: int,
) -> PageResponse[TestCaseListResponse]:
    statement = select(TestCaseRow)
    if search.strip():
        term = search.strip()
        statement = statement.where(or_(
            TestCaseRow.id.icontains(term, autoescape=True),
            TestCaseRow.name.icontains(term, autoescape=True),
            TestCaseRow.source_text.icontains(term, autoescape=True),
        ))
    latest_version = (
        select(func.max(TestPlanRow.version_number))
        .where(TestPlanRow.test_case_id == TestCaseRow.id).correlate(TestCaseRow).scalar_subquery()
    )
    async with read_snapshot(sessions) as session:
        total = await session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = (await session.execute(
            statement.add_columns(latest_version)
            .order_by(TestCaseRow.created_at.desc(), TestCaseRow.id.desc()).limit(limit).offset(offset)
        )).all()
    return PageResponse[TestCaseListResponse](items=[
        TestCaseListResponse.model_validate({
            **test_case_from_row(row).model_dump(), "latest_plan_version": version,
        }) for row, version in rows
    ], total=int(total or 0))


def filtered_test_runs(filters: TestRunFilterQuery) -> Select[tuple[TestRunRow]]:
    statement = (
        select(TestRunRow).select_from(TestRunRow)
        .join(TestPlanRow, TestRunRow.test_plan_id == TestPlanRow.id)
        .join(TestCaseRow, TestPlanRow.test_case_id == TestCaseRow.id)
    )
    if filters.search.strip():
        term = filters.search.strip()
        statement = statement.where(or_(
            TestRunRow.id.icontains(term, autoescape=True),
            TestCaseRow.id.icontains(term, autoescape=True),
            TestCaseRow.name.icontains(term, autoescape=True),
            TestCaseRow.source_text.icontains(term, autoescape=True),
        ))
    if filters.test_case_id:
        statement = statement.where(TestPlanRow.test_case_id == filters.test_case_id)
    if filters.status:
        statement = statement.where(TestRunRow.status.in_(filters.status))
    if filters.verdict:
        statement = statement.where(TestRunRow.verdict == filters.verdict)
    # SQLite persists naive UTC; normalize offsets before binding comparisons.
    if filters.created_from:
        statement = statement.where(TestRunRow.created_at >= filters.created_from.astimezone(timezone.utc).replace(tzinfo=None))
    if filters.created_before:
        statement = statement.where(TestRunRow.created_at < filters.created_before.astimezone(timezone.utc).replace(tzinfo=None))
    return statement


async def list_test_runs(
    sessions: async_sessionmaker[AsyncSession], filters: TestRunPageQuery,
) -> PageResponse[TestRunListResponse]:
    statement = filtered_test_runs(filters)
    async with read_snapshot(sessions) as session:
        total = await session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = (await session.execute(
            statement.options(defer(TestRunRow.snapshot_json, raiseload=True))
            .add_columns(TestCaseRow.id, TestCaseRow.name)
            .order_by(TestRunRow.created_at.desc(), TestRunRow.id.desc())
            .limit(filters.limit).offset(filters.offset)
        )).all()
        if not rows:
            return PageResponse[TestRunListResponse](items=[], total=int(total or 0))
        run_ids = [run.id for run, _, _ in rows]
        plan_ids = [run.test_plan_id for run, _, _ in rows]
        planned_counts = dict((await session.execute(
            select(TestTaskRow.test_plan_id, func.count()).where(TestTaskRow.test_plan_id.in_(plan_ids))
            .group_by(TestTaskRow.test_plan_id)
        )).tuples().all())
        task_counts: dict[str, dict[TaskRunStatus, int]] = {}
        for run_id, status, count in (await session.execute(
            select(TaskRunRow.test_run_id, TaskRunRow.status, func.count())
            .where(TaskRunRow.test_run_id.in_(run_ids)).group_by(TaskRunRow.test_run_id, TaskRunRow.status)
        )).all():
            task_counts.setdefault(run_id, {})[status] = count
        screenshot_counts = dict((await session.execute(
            select(TaskRunRow.test_run_id, func.count(ArtifactRow.id)).join(ArtifactRow)
            .where(TaskRunRow.test_run_id.in_(run_ids), ArtifactRow.type == ArtifactType.SCREENSHOT)
            .group_by(TaskRunRow.test_run_id)
        )).tuples().all())
    return PageResponse[TestRunListResponse](items=[
        TestRunListResponse.model_validate({
            **test_run_from_row(run).model_dump(),
            "test_case_id": case_id, "test_case_name": case_name,
            "task_count": planned_counts.get(run.test_plan_id, 0),
            "task_status_counts": task_counts.get(run.id, {}),
            "screenshot_count": screenshot_counts.get(run.id, 0),
        }) for run, case_id, case_name in rows
    ], total=int(total or 0))


async def test_run_statistics(
    sessions: async_sessionmaker[AsyncSession], filters: TestRunFilterQuery,
) -> TestRunStatisticsResponse:
    runs = filtered_test_runs(filters).subquery()
    duration = case((
        (runs.c.status == TestRunStatus.FINISHED)
        & runs.c.started_at.is_not(None) & runs.c.finished_at.is_not(None),
        (func.julianday(runs.c.finished_at) - func.julianday(runs.c.started_at)) * 86400.0,
    ), else_=None)
    day = func.date(runs.c.created_at, "+8 hours")
    async with read_snapshot(sessions) as session:
        total, average = (await session.execute(select(func.count(), func.avg(duration)).select_from(runs))).one()
        counts = dict((await session.execute(
            select(runs.c.verdict, func.count()).where(runs.c.verdict.is_not(None)).group_by(runs.c.verdict)
        )).tuples().all())
        daily_rows = (await session.execute(
            select(day, func.count(), func.avg(duration), func.coalesce(func.sum(duration), 0))
            .select_from(runs).group_by(day).order_by(day)
        )).all()
    return TestRunStatisticsResponse(
        run_count=total, verdict_counts={verdict: counts.get(verdict, 0) for verdict in TestRunVerdict},
        average_duration_seconds=average,
        daily=[TestRunDailyStatistics(date=day_value, run_count=count,
            average_duration_seconds=average_duration, total_duration_seconds=total_duration)
            for day_value, count, average_duration, total_duration in daily_rows],
    )


async def create_test_case(sessions: async_sessionmaker[AsyncSession], content: TestCaseContent) -> TestCase:
    row = TestCaseRow(
        id=str(uuid4()), name=content.name, source_text=content.source_text
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()
    return test_case_from_row(row)


async def list_test_plans(
    sessions: async_sessionmaker[AsyncSession], test_case_id: str,
) -> list[TestPlan]:
    async with read_snapshot(sessions) as session:
        if await session.get(TestCaseRow, test_case_id) is None:
            raise LookupError("Test case not found")
        plans = (await session.scalars(
            select(TestPlanRow).where(TestPlanRow.test_case_id == test_case_id)
            .order_by(TestPlanRow.version_number.desc())
        )).all()
        if not plans:
            return []
        tasks_by_plan: dict[str, list[TestTaskRow]] = {}
        tasks = await session.scalars(
            select(TestTaskRow).where(TestTaskRow.test_plan_id.in_([plan.id for plan in plans]))
            .order_by(TestTaskRow.test_plan_id, TestTaskRow.position)
        )
        for task in tasks:
            tasks_by_plan.setdefault(task.test_plan_id, []).append(task)
        return [test_plan_from_rows(plan, tasks_by_plan.get(plan.id, [])) for plan in plans]


async def read_run_events(
    session: AsyncSession, test_run_id: str, *, after: int = 0,
) -> list[StoredRunEvent]:
    rows = await session.scalars(
        select(RunEventRow).where(
            RunEventRow.test_run_id == test_run_id, RunEventRow.sequence > after,
        ).order_by(RunEventRow.sequence)
    )
    return [stored_event_from_row(row) for row in rows]


async def list_events(
    sessions: async_sessionmaker[AsyncSession], test_run_id: str, *, after: int = 0,
) -> list[StoredRunEvent]:
    async with read_snapshot(sessions) as session:
        if await session.scalar(select(TestRunRow.id).where(TestRunRow.id == test_run_id)) is None:
            raise LookupError("Test run not found")
        return await read_run_events(session, test_run_id, after=after)


async def read_test_run_report(
    sessions: async_sessionmaker[AsyncSession], test_run_id: str,
) -> TestRunReport:
    async with read_snapshot(sessions) as session:
        detail = await read_test_run_detail(session, test_run_id)
        events = await read_run_events(session, test_run_id)
        task_ids = select(TaskRunRow.id).where(TaskRunRow.test_run_id == test_run_id)
        artifacts = await session.scalars(
            select(ArtifactRow).where(
                (ArtifactRow.test_run_id == test_run_id) | ArtifactRow.task_run_id.in_(task_ids)
            ).order_by(ArtifactRow.created_at)
        )
        return TestRunReport(detail=detail, events=events,
                             artifacts=[artifact_from_row(row) for row in artifacts])
