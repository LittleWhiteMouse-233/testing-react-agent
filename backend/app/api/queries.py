"""Read-only API projections over existing persisted facts.

These joins and aggregates serve browser lists, not execution decisions. Public
SQLAlchemy SELECT operations fill the API gap without changing repository writes
or domain entities. Separate aggregate queries avoid multiplying counts through
task/artifact joins. No projection is persisted.
"""
from __future__ import annotations

from datetime import timezone

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.schemas import (
    PageResponse, TestCaseListResponse, TestRunDailyStatistics, TestRunFilterQuery,
    TestRunListResponse, TestRunPageQuery, TestRunStatisticsResponse,
)
from app.domain.execution import ArtifactType, TaskRunStatus, TestRunStatus, TestRunVerdict
from app.persistence.mappers import test_case_from_row, test_run_from_row
from app.persistence.models import ArtifactRow, TaskRunRow, TestCaseRow, TestPlanRow, TestRunRow, TestTaskRow


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
    async with sessions() as session:
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
    async with sessions() as session:
        total = await session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = (await session.execute(
            statement.add_columns(TestCaseRow.id, TestCaseRow.name)
            .order_by(TestRunRow.created_at.desc(), TestRunRow.id.desc())
            .limit(filters.limit).offset(filters.offset)
        )).all()
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
    async with sessions() as session:
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
