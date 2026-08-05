from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select

from app.api.schemas import (
    ExportCreate,
    ArtifactResponse,
    CancelResponse,
    DeviceHealthResponse,
    DeviceSummaryResponse,
    PageResponse,
    PlanCreate,
    PlanRevisionResponse,
    PlanRevisionCreate,
    ReportResponse,
    RunCreate,
    RunDetailResponse,
    RunResponse,
    StepEventResponse,
    TestCaseCreate,
    TestCaseResponse,
)
from app.container import Container
from app.domain.planning import PlanOutput, PlanRequest
from app.persistence.models import (
    ArtifactRow,
    PlanRevisionRow,
    StepEventRow,
    TaskRunRow,
    TestCaseRow,
    TestRunRow,
)
from app.llm.contracts import ModelActivity
from app.services.events import serialize_event
from app.services.reporting import artifact_dict
from app.services.run_service import RunConflict


router = APIRouter(prefix="/api")


def container(request: Request) -> Container:
    return request.app.state.container


def case_dict(row: TestCaseRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "source_text": row.source_text,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def plan_dict(row: PlanRevisionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "test_case_id": row.test_case_id,
        "revision": row.revision,
        "source": row.source,
        "parent_revision_id": row.parent_revision_id,
        "plan": row.plan_json,
        "model_info": row.model_info_json,
        "created_at": row.created_at.isoformat(),
    }


def run_dict(row: TestRunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "test_case_id": row.test_case_id,
        "plan_revision_id": row.plan_revision_id,
        "device_id": row.device_id,
        "status": row.status,
        "overall_result": row.overall_result,
        "snapshot": row.snapshot_json,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/test-cases", status_code=201, response_model=TestCaseResponse)
async def create_test_case(payload: TestCaseCreate, request: Request) -> dict[str, Any]:
    app = container(request)
    row = TestCaseRow(
        id=str(uuid4()), name=payload.name, source_text=payload.source_text
    )
    async with app.sessions() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return case_dict(row)


@router.get("/test-cases", response_model=PageResponse[TestCaseResponse])
async def list_test_cases(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    app = container(request)
    async with app.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(TestCaseRow))
        rows = list(
            (
                await session.scalars(
                    select(TestCaseRow)
                    .order_by(TestCaseRow.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
    return {"items": [case_dict(row) for row in rows], "total": total}


@router.get("/test-cases/{test_case_id}", response_model=TestCaseResponse)
async def get_test_case(test_case_id: str, request: Request) -> dict[str, Any]:
    app = container(request)
    async with app.sessions() as session:
        row = await session.get(TestCaseRow, test_case_id)
    if not row:
        raise HTTPException(404, "Test case not found")
    return case_dict(row)


@router.post(
    "/test-cases/{test_case_id}/plans",
    status_code=201,
    response_model=PlanRevisionResponse,
)
async def create_plan(
    test_case_id: str, payload: PlanCreate, request: Request
) -> dict[str, Any]:
    app = container(request)
    async with app.sessions() as session:
        test_case = await session.get(TestCaseRow, test_case_id)
        if not test_case:
            raise HTTPException(404, "Test case not found")
        current_revision = await session.scalar(
            select(func.coalesce(func.max(PlanRevisionRow.revision), 0)).where(
                PlanRevisionRow.test_case_id == test_case_id
            )
        )
        revision = (current_revision or 0) + 1
    try:
        plan = await app.planning_graph.generate(
            PlanRequest(
                test_case_id=test_case_id,
                text=test_case.source_text,
                device_profile=payload.device_profile,
            )
        )
    except Exception as exc:
        raise HTTPException(502, f"Planning failed: {exc}") from exc
    row = PlanRevisionRow(
        id=str(uuid4()),
        test_case_id=test_case_id,
        revision=revision,
        source="llm",
        plan_json=plan.model_dump(mode="json"),
        model_info_json=app.model_registry.for_activity(
            ModelActivity.PLANNING
        ).model_snapshot.model_dump(mode="json"),
    )
    async with app.sessions() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return plan_dict(row)


@router.get(
    "/test-cases/{test_case_id}/plans",
    response_model=PageResponse[PlanRevisionResponse],
)
async def list_plans(test_case_id: str, request: Request) -> dict[str, Any]:
    app = container(request)
    async with app.sessions() as session:
        exists = await session.get(TestCaseRow, test_case_id)
        if not exists:
            raise HTTPException(404, "Test case not found")
        rows = list(
            (
                await session.scalars(
                    select(PlanRevisionRow)
                    .where(PlanRevisionRow.test_case_id == test_case_id)
                    .order_by(PlanRevisionRow.revision.desc())
                )
            ).all()
        )
    return {"items": [plan_dict(row) for row in rows], "total": len(rows)}


@router.post(
    "/plan-revisions/{plan_revision_id}/revisions",
    status_code=201,
    response_model=PlanRevisionResponse,
)
async def revise_plan(
    plan_revision_id: str, payload: PlanRevisionCreate, request: Request
) -> dict[str, Any]:
    app = container(request)
    # Pydantic has already applied all PlanOutput invariants.
    plan = PlanOutput.model_validate(payload.plan)
    async with app.sessions() as session:
        parent = await session.get(PlanRevisionRow, plan_revision_id)
        if not parent:
            raise HTTPException(404, "Plan revision not found")
        current_revision = await session.scalar(
            select(func.max(PlanRevisionRow.revision)).where(
                PlanRevisionRow.test_case_id == parent.test_case_id
            )
        )
        next_revision = (current_revision or 0) + 1
        row = PlanRevisionRow(
            id=str(uuid4()),
            test_case_id=parent.test_case_id,
            revision=next_revision,
            source="manual",
            parent_revision_id=parent.id,
            plan_json=plan.model_dump(mode="json"),
            model_info_json=parent.model_info_json,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return plan_dict(row)


@router.post("/runs", status_code=201, response_model=RunResponse)
async def create_run(payload: RunCreate, request: Request) -> dict[str, Any]:
    app = container(request)
    try:
        row = await app.run_service.start(
            plan_revision_id=payload.plan_revision_id,
            device_id=payload.device_id,
            confirmed_assumptions=payload.confirmed_assumptions,
        )
    except RunConflict as exc:
        raise HTTPException(
            409,
            {"message": str(exc), "active_run_id": exc.active_run_id},
        ) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return run_dict(row)


@router.get("/runs", response_model=PageResponse[RunResponse])
async def list_runs(
    request: Request,
    test_case_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    app = container(request)
    statement = select(TestRunRow)
    count_statement = select(func.count()).select_from(TestRunRow)
    if test_case_id:
        statement = statement.where(TestRunRow.test_case_id == test_case_id)
        count_statement = count_statement.where(TestRunRow.test_case_id == test_case_id)
    async with app.sessions() as session:
        total = await session.scalar(count_statement)
        rows = list(
            (
                await session.scalars(
                    statement.order_by(TestRunRow.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
    return {"items": [run_dict(row) for row in rows], "total": total}


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    app = container(request)
    async with app.sessions() as session:
        row = await session.get(TestRunRow, run_id)
        if not row:
            raise HTTPException(404, "Run not found")
        tasks = list(
            (
                await session.scalars(
                    select(TaskRunRow)
                    .where(TaskRunRow.test_run_id == run_id)
                    .order_by(TaskRunRow.task_index)
                )
            ).all()
        )
    result = run_dict(row)
    result["task_runs"] = [
        {
            "id": item.id,
            "task": item.task_json,
            "task_index": item.task_index,
            "status": item.status,
            "cycle_count": item.cycle_count,
            "outcome": item.outcome_json,
        }
        for item in tasks
    ]
    return result


@router.post("/runs/{run_id}/cancel", response_model=CancelResponse)
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    app = container(request)
    try:
        accepted = await app.run_service.cancel(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"run_id": run_id, "cancel_requested": accepted}


@router.get(
    "/runs/{run_id}/events",
    response_model=PageResponse[StepEventResponse],
)
async def list_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    app = container(request)
    async with app.sessions() as session:
        exists = await session.get(TestRunRow, run_id)
        if not exists:
            raise HTTPException(404, "Run not found")
        rows = list(
            (
                await session.scalars(
                    select(StepEventRow)
                    .where(
                        StepEventRow.test_run_id == run_id,
                        StepEventRow.sequence > after,
                    )
                    .order_by(StepEventRow.sequence)
                )
            ).all()
        )
    return {"items": [serialize_event(row) for row in rows], "total": len(rows)}


@router.get("/runs/{run_id}/stream")
async def stream_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    app = container(request)
    async with app.sessions() as session:
        if not await session.get(TestRunRow, run_id):
            raise HTTPException(404, "Run not found")

    async def generate() -> AsyncIterator[str]:
        cursor = after
        async with app.event_bus.subscribe(run_id) as queue:
            while True:
                if await request.is_disconnected():
                    break
                async with app.sessions() as session:
                    rows = list(
                        (
                            await session.scalars(
                                select(StepEventRow)
                                .where(
                                    StepEventRow.test_run_id == run_id,
                                    StepEventRow.sequence > cursor,
                                )
                                .order_by(StepEventRow.sequence)
                            )
                        ).all()
                    )
                for row in rows:
                    cursor = row.sequence
                    data = json.dumps(serialize_event(row), ensure_ascii=False)
                    yield f"id: {row.sequence}\nevent: {row.type}\ndata: {data}\n\n"
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/report", response_model=ReportResponse)
async def get_report(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return await container(request).reports.build(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/runs/{run_id}/exports",
    status_code=201,
    response_model=ArtifactResponse,
)
async def create_export(
    run_id: str, payload: ExportCreate, request: Request
) -> dict[str, Any]:
    try:
        row = await container(request).reports.export(run_id, payload.format)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return artifact_dict(row)


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request) -> FileResponse:
    app = container(request)
    async with app.sessions() as session:
        row = await session.get(ArtifactRow, artifact_id)
    if not row:
        raise HTTPException(404, "Artifact not found")
    try:
        path = app.artifacts.resolve(row.relative_path)
    except ValueError as exc:
        raise HTTPException(500, "Invalid artifact path") from exc
    if not path.is_file():
        raise HTTPException(404, "Artifact file not found")
    disposition = "inline" if row.type == "screenshot" else "attachment"
    return FileResponse(
        path,
        media_type=row.mime_type,
        filename=path.name if disposition == "attachment" else None,
        content_disposition_type=disposition,
    )


@router.get("/devices", response_model=PageResponse[DeviceSummaryResponse])
async def list_devices(request: Request) -> dict[str, Any]:
    app = container(request)
    items = []
    for device_id, device in app.devices.items():
        health = await device.health()
        items.append(
            {
                "id": device_id,
                "type": type(device).__name__,
                "health": health.model_dump(mode="json"),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/devices/{device_id}/health", response_model=DeviceHealthResponse)
async def device_health(device_id: str, request: Request) -> dict[str, Any]:
    device = container(request).devices.get(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    health = await device.health()
    capabilities = await device.capabilities() if health.available else None
    return {
        "id": device_id,
        "health": health.model_dump(mode="json"),
        "capabilities": capabilities.model_dump(mode="json") if capabilities else None,
    }
