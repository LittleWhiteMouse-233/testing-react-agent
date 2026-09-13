from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    ApiError,
    PageResponse,
    ReportExportRequest,
    ReviseTestPlanRequest,
    TestCaseCreateRequest,
    TestRunCreateRequest,
)
from app.container import Container
from app.domain.execution import Artifact, StoredRunEvent, TestRun, TestRunDetail, TestRunStatus
from app.domain.ids import ArtifactId, TestCaseId, TestPlanId, TestRunId
from app.domain.planning import TestCase, TestPlan
from app.reporting import TestRunReport
from app.execution.run_service import RunConflict, RunResourcesUnavailable
from app.tools import MCPConnectionError


API_ERROR_RESPONSE: dict[str, Any] = {"model": ApiError}
NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: API_ERROR_RESPONSE
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: API_ERROR_RESPONSE
}
BAD_GATEWAY_RESPONSE: dict[int | str, dict[str, Any]] = {
    502: API_ERROR_RESPONSE
}

router = APIRouter(
    prefix="/api",
    responses={422: API_ERROR_RESPONSE, 500: API_ERROR_RESPONSE},
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


@router.get(
    "/test-cases/{test_case_id}",
    response_model=TestCase,
    responses=NOT_FOUND_RESPONSE,
)
async def get_test_case(test_case_id: TestCaseId, request: Request) -> TestCase:
    try:
        return await container(request).repository.get_test_case(test_case_id)
    except LookupError as exc:
        raise problem(404, "test_case_not_found", str(exc)) from exc


@router.post(
    "/test-cases/{test_case_id}/plans",
    status_code=201,
    response_model=TestPlan,
    responses={**NOT_FOUND_RESPONSE, **BAD_GATEWAY_RESPONSE},
)
async def generate_plan(
    test_case_id: TestCaseId, request: Request
) -> TestPlan:
    try:
        return await container(request).planning.generate(
            test_case_id=test_case_id
        )
    except LookupError as exc:
        raise problem(404, "planning_input_not_found", str(exc)) from exc
    except Exception as exc:
        raise problem(502, "planning_failed", str(exc)) from exc


@router.get(
    "/test-cases/{test_case_id}/plans",
    response_model=PageResponse[TestPlan],
    responses=NOT_FOUND_RESPONSE,
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
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
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


@router.post(
    "/runs",
    status_code=201,
    response_model=TestRun,
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE, **BAD_GATEWAY_RESPONSE},
)
async def create_test_run(
    payload: TestRunCreateRequest, request: Request
) -> TestRun:
    try:
        return await container(request).run_service.start(
            test_plan_id=payload.test_plan_id,
            assumptions_confirmed=payload.assumptions_confirmed,
        )
    except MCPConnectionError as exc:
        raise problem(502, "mcp_unavailable", str(exc)) from exc
    except RunConflict as exc:
        raise problem(409, "active_run_exists", str(exc)) from exc
    except RunResourcesUnavailable as exc:
        raise problem(409, "run_resources_unavailable", str(exc)) from exc
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


@router.get(
    "/runs/{test_run_id}",
    response_model=TestRunDetail,
    responses=NOT_FOUND_RESPONSE,
)
async def get_test_run(test_run_id: TestRunId, request: Request) -> TestRunDetail:
    try:
        return await container(request).repository.get_test_run_detail(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc


@router.post(
    "/runs/{test_run_id}/cancel",
    status_code=202,
    response_class=Response,
    response_description="Cancellation request accepted",
    responses=NOT_FOUND_RESPONSE,
)
async def cancel_test_run(test_run_id: TestRunId, request: Request) -> Response:
    try:
        await container(request).run_service.cancel(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc
    return Response(status_code=202)


@router.get(
    "/runs/{test_run_id}/events",
    response_model=PageResponse[StoredRunEvent],
    responses=NOT_FOUND_RESPONSE,
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


@router.get(
    "/runs/{test_run_id}/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-sent run events",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **NOT_FOUND_RESPONSE,
    },
)
async def stream_run_events(
    test_run_id: TestRunId,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: int | None = Header(
        default=None,
        alias="Last-Event-ID",
        ge=0,
    ),
) -> StreamingResponse:
    app = container(request)
    try:
        await app.repository.get_test_run(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc

    async def generate() -> AsyncIterator[str]:
        cursor = max(after, last_event_id or 0)
        yield "retry: 1000\n\n"
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


@router.get(
    "/runs/{test_run_id}/report",
    response_model=TestRunReport,
    responses=NOT_FOUND_RESPONSE,
)
async def get_test_run_report(
    test_run_id: TestRunId, request: Request
) -> TestRunReport:
    try:
        return await container(request).reports.build(test_run_id)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc


@router.post(
    "/runs/{test_run_id}/exports",
    status_code=201,
    response_model=Artifact,
    responses=NOT_FOUND_RESPONSE,
)
async def export_test_run_report(
    test_run_id: TestRunId, payload: ReportExportRequest, request: Request
) -> Artifact:
    try:
        return await container(request).reports.export(test_run_id, payload.format)
    except LookupError as exc:
        raise problem(404, "test_run_not_found", str(exc)) from exc


@router.get(
    "/artifacts/{artifact_id}",
    response_class=Response,
    responses={
        200: {
            "description": "Stored artifact content",
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "application/json": {"schema": {}},
                "text/html": {"schema": {"type": "string"}},
            },
        },
        **NOT_FOUND_RESPONSE,
    },
)
async def get_artifact(artifact_id: ArtifactId, request: Request) -> Response:
    try:
        content, mime_type = await container(request).artifacts.load_content(artifact_id)
    except LookupError as exc:
        raise problem(404, "artifact_not_found", str(exc)) from exc
    return Response(content=content, media_type=mime_type)
