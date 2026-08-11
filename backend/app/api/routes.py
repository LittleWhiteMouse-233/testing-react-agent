from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    ApiError,
    GeneratePlanRequest,
    PageResponse,
    ReportExportRequest,
    ReviseTestPlanRequest,
    TestCaseCreateRequest,
    TestRunCreateRequest,
)
from app.container import Container
from app.domain.artifacts import Artifact
from app.domain.events import StoredRunEvent
from app.domain.execution import TestRun, TestRunStatus
from app.domain.ids import ArtifactId, DeviceId, TestCaseId, TestPlanId, TestRunId
from app.domain.planning import TestPlan
from app.domain.test_cases import TestCase
from app.services.read_models import DeviceView, TestRunDetail, TestRunReport
from app.services.run_service import RunConflict


router = APIRouter(
    prefix="/api",
    responses={
        404: {"model": ApiError},
        409: {"model": ApiError},
        422: {"model": ApiError},
        502: {"model": ApiError},
        500: {"model": ApiError},
    },
)


def container(request: Request) -> Container:
    return request.app.state.container


def problem(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


@router.post("/test-cases", status_code=201, response_model=TestCase)
async def create_test_case(
    payload: TestCaseCreateRequest, request: Request
) -> TestCase:
    return await container(request).repository.create_test_case(payload)


@router.get("/test-cases", response_model=PageResponse[TestCase])
async def list_test_cases(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageResponse[TestCase]:
    items, total = await container(request).repository.list_test_cases(
        limit=limit, offset=offset
    )
    return PageResponse[TestCase](items=items, total=total)


@router.get("/test-cases/{test_case_id}", response_model=TestCase)
async def get_test_case(test_case_id: TestCaseId, request: Request) -> TestCase:
    try:
        return await container(request).repository.get_test_case(test_case_id)
    except LookupError as exc:
        raise problem(404, "test_case_not_found", str(exc)) from exc


@router.post(
    "/test-cases/{test_case_id}/plans", status_code=201, response_model=TestPlan
)
async def generate_plan(
    test_case_id: TestCaseId, payload: GeneratePlanRequest, request: Request
) -> TestPlan:
    try:
        return await container(request).planning.generate(
            test_case_id=test_case_id, device_id=payload.device_id
        )
    except LookupError as exc:
        raise problem(404, "planning_input_not_found", str(exc)) from exc
    except Exception as exc:
        raise problem(502, "planning_failed", str(exc)) from exc


@router.get(
    "/test-cases/{test_case_id}/plans", response_model=PageResponse[TestPlan]
)
async def list_test_plans(
    test_case_id: TestCaseId, request: Request
) -> PageResponse[TestPlan]:
    try:
        items = await container(request).repository.list_test_plans(test_case_id)
    except LookupError as exc:
        raise problem(404, "test_case_not_found", str(exc)) from exc
    return PageResponse[TestPlan](items=items, total=len(items))


@router.post(
    "/test-plans/{test_plan_id}/revisions",
    status_code=201,
    response_model=TestPlan,
)
async def revise_test_plan(
    test_plan_id: TestPlanId, payload: ReviseTestPlanRequest, request: Request
) -> TestPlan:
    try:
        return await container(request).planning.revise(
            latest_test_plan_id=test_plan_id, content=payload.content
        )
    except LookupError as exc:
        raise problem(404, "test_plan_not_found", str(exc)) from exc
    except ValueError as exc:
        raise problem(409, "test_plan_not_latest", str(exc)) from exc


@router.post("/runs", status_code=201, response_model=TestRun)
async def create_test_run(
    payload: TestRunCreateRequest, request: Request
) -> TestRun:
    try:
        return await container(request).run_service.start(
            test_plan_id=payload.test_plan_id,
            device_id=payload.device_id,
            assumptions_confirmed=payload.assumptions_confirmed,
        )
    except RunConflict as exc:
        raise problem(409, "active_run_exists", str(exc)) from exc
    except LookupError as exc:
        raise problem(404, "run_input_not_found", str(exc)) from exc
    except ValueError as exc:
        raise problem(409, "test_plan_not_startable", str(exc)) from exc


@router.get("/runs", response_model=PageResponse[TestRun])
async def list_test_runs(
    request: Request,
    test_case_id: TestCaseId | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageResponse[TestRun]:
    items, total = await container(request).repository.list_test_runs(
        test_case_id=test_case_id, limit=limit, offset=offset
    )
    return PageResponse[TestRun](items=items, total=total)


@router.get("/runs/{test_run_id}", response_model=TestRunDetail)
async def get_test_run(test_run_id: TestRunId, request: Request) -> TestRunDetail:
    try:
        return await container(request).repository.get_detail(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc


@router.post("/runs/{test_run_id}/cancel", status_code=202)
async def cancel_test_run(test_run_id: TestRunId, request: Request) -> Response:
    try:
        await container(request).run_service.cancel(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc
    return Response(status_code=202)


@router.get(
    "/runs/{test_run_id}/events",
    response_model=PageResponse[StoredRunEvent],
)
async def list_run_events(
    test_run_id: TestRunId,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> PageResponse[StoredRunEvent]:
    try:
        items = await container(request).repository.list_events(
            test_run_id, after=after
        )
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc
    return PageResponse[StoredRunEvent](items=items, total=len(items))


@router.get("/runs/{test_run_id}/stream")
async def stream_run_events(
    test_run_id: TestRunId,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    app = container(request)
    try:
        await app.repository.get_test_run(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc

    async def generate() -> AsyncIterator[str]:
        cursor = after
        async with app.event_bus.subscribe(test_run_id) as queue:
            while True:
                if await request.is_disconnected():
                    return
                events = await app.repository.list_events(test_run_id, after=cursor)
                for stored in events:
                    cursor = stored.sequence
                    data = json.dumps(stored.model_dump(mode="json"), ensure_ascii=False)
                    yield f"id: {stored.sequence}\ndata: {data}\n\n"
                run = await app.repository.get_test_run(test_run_id)
                if run.status == TestRunStatus.FINISHED:
                    trailing = await app.repository.list_events(
                        test_run_id, after=cursor
                    )
                    if trailing:
                        continue
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{test_run_id}/report", response_model=TestRunReport)
async def get_test_run_report(
    test_run_id: TestRunId, request: Request
) -> TestRunReport:
    try:
        return await container(request).reports.build(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc


@router.post(
    "/runs/{test_run_id}/exports", status_code=201, response_model=Artifact
)
async def export_test_run_report(
    test_run_id: TestRunId, payload: ReportExportRequest, request: Request
) -> Artifact:
    try:
        return await container(request).reports.export(test_run_id, payload.format)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: ArtifactId, request: Request) -> Response:
    try:
        content, mime_type = await container(request).artifacts.load_content(artifact_id)
    except LookupError as exc:
        raise problem(404, "artifact_not_found", str(exc)) from exc
    return Response(content=content, media_type=mime_type)


async def _device_view(app: Container, device_id: DeviceId) -> DeviceView:
    device = app.devices[device_id]
    health_result, info_result = await asyncio.gather(
        device.health(), device.describe(), return_exceptions=True
    )
    if isinstance(health_result, BaseException):
        from app.domain.device import DeviceHealth

        health = DeviceHealth(available=False, message=str(health_result))
    else:
        health = health_result
    info = None if isinstance(info_result, BaseException) else info_result
    return DeviceView(
        device_id=device_id,
        provider=device.provider,
        health=health,
        info=info,
        tool_catalog=app.tools.snapshot_for(device_id),
    )


@router.get("/devices", response_model=PageResponse[DeviceView])
async def list_devices(request: Request) -> PageResponse[DeviceView]:
    app = container(request)
    items = await asyncio.gather(
        *(_device_view(app, device_id) for device_id in app.devices)
    )
    return PageResponse[DeviceView](items=list(items), total=len(items))


@router.get("/devices/{device_id}", response_model=DeviceView)
async def get_device(device_id: DeviceId, request: Request) -> DeviceView:
    app = container(request)
    if device_id not in app.devices:
        raise problem(404, "device_not_found", "Device not found")
    return await _device_view(app, device_id)
