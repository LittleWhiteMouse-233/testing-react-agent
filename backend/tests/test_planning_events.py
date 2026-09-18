from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select

from app.api.routes import stream_planning_events
from app.container import Container
from app.domain.errors import PlanningFailure, TestPlanNotLatest as PlanNotLatest
from app.llm import ScriptedChatModelClient
from app.persistence.models import RunEventRow
from mcp_support import build_client


def test_planning_stream_ready_precedes_work_and_has_no_history(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        content = app.openapi()["paths"]["/api/test-cases/{test_case_id}/planning/stream"]["get"]["responses"]["200"]["content"]
        assert set(content) == {"text/event-stream"}
        assert content["text/event-stream"]["schema"]["$ref"].endswith("/PlanningEvent")
        case = client.post("/api/test-cases", json={"name": "进度", "source_text": "检查设置"}).json()
        case_id = case["id"]
        request_id = str(uuid4())

        async def exercise() -> None:
            async def receive():
                await asyncio.Event().wait()
                return {"type": "http.disconnect"}

            request = Request({"type": "http", "app": app}, receive=receive)
            response = await stream_planning_events(case_id, request)
            stream = cast(AsyncGenerator[str, None], response.body_iterator)
            assert "event: ready" in await anext(stream)
            # The real route remains subscribed before the POST begins, even for a fast model.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                result = await api.post(f"/api/test-cases/{case_id}/plans", json={"planning_request_id": request_id})
            assert result.status_code == 201
            events = [json.loads((await anext(stream)).removeprefix("data: ")) for _ in range(6)]
            assert [event["type"] for event in events] == [
                "planning.started", "planning.input_ready", "planning.attempt_started",
                "planning.validated", "planning.saving", "planning.succeeded",
            ]
            assert all(event["planning_request_id"] == request_id and event["test_case_id"] == case_id for event in events)
            saved_plan = await container.repository.get_latest_test_plan(case_id)
            assert saved_plan is not None and saved_plan.id == result.json()["id"]
            async with container.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(RunEventRow)) == 0
            await stream.aclose()
            # A fresh subscription receives ready only; no retained event history.
            async with container.planning_event_bus.subscribe(case_id) as fresh:
                assert fresh.empty()

        assert client.portal is not None
        client.portal.call(exercise)


@pytest.mark.parametrize("invalid_attempts", [1, 3])
def test_planning_attempt_events_and_failed_plan_is_not_saved(tmp_path: Path, invalid_attempts: int) -> None:
    with build_client(tmp_path) as client:
        container = cast(Container, cast(FastAPI, client.app).state.container)
        container.model_provider.replace_client_for_testing("default", ScriptedChatModelClient(
            plans=[{"title": "缺少任务"} for _ in range(invalid_attempts)],
        ))
        case_id = client.post("/api/test-cases", json={"name": "重试", "source_text": "检查设置"}).json()["id"]

        async def exercise() -> None:
            async with container.planning_event_bus.subscribe(case_id) as queue:
                if invalid_attempts == 3:
                    with pytest.raises(PlanningFailure):
                        await container.planning.generate(test_case_id=case_id, planning_request_id=str(uuid4()))
                    assert await container.repository.get_latest_test_plan(case_id) is None
                else:
                    await container.planning.generate(test_case_id=case_id, planning_request_id=str(uuid4()))
                events = [queue.get_nowait() for _ in range(queue.qsize())]
                failures = [event for event in events if event is not None and event.type == "planning.attempt_failed"]
                assert len(failures) == invalid_attempts
                assert all("校验失败" in event.reason for event in failures)
                assert [event.attempt for event in failures] == list(range(1, invalid_attempts + 1))
                assert events[-1] is not None
                assert events[-1].type == ("planning.failed" if invalid_attempts == 3 else "planning.succeeded")

        assert client.portal is not None
        client.portal.call(exercise)


def test_disconnected_subscription_does_not_cancel_planning_and_save_conflict_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        container = cast(Container, cast(FastAPI, client.app).state.container)
        case_id = client.post("/api/test-cases", json={"name": "断线", "source_text": "检查设置"}).json()["id"]
        entered, release = asyncio.Event(), asyncio.Event()
        original_generate = container.planning_graph.generate

        async def delayed_generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_generate(*args, **kwargs)

        monkeypatch.setattr(container.planning_graph, "generate", delayed_generate)

        async def exercise() -> None:
            async with container.planning_event_bus.subscribe(case_id):
                planning = asyncio.create_task(container.planning.generate(test_case_id=case_id, planning_request_id=str(uuid4())))
                await asyncio.wait_for(entered.wait(), 5)
            release.set()
            plan = await planning
            saved_plan = await container.repository.get_latest_test_plan(case_id)
            assert saved_plan is not None and saved_plan.id == plan.id

            async def conflict(**kwargs):
                raise PlanNotLatest("计划版本冲突")

            monkeypatch.setattr(container.repository, "create_plan", conflict)
            async with container.planning_event_bus.subscribe(case_id) as queue:
                with pytest.raises(PlanNotLatest):
                    await container.planning.generate(test_case_id=case_id, planning_request_id=str(uuid4()), user_input="细化")
                events = [queue.get_nowait() for _ in range(queue.qsize())]
                assert not any(event and event.type == "planning.succeeded" for event in events)
                assert events[-1] is not None and events[-1].type == "planning.failed"
                saved_plan = await container.repository.get_latest_test_plan(case_id)
                assert saved_plan is not None and saved_plan.id == plan.id

        assert client.portal is not None
        client.portal.call(exercise)


def test_planning_request_requires_valid_correlation_id(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        case_id = client.post("/api/test-cases", json={"name": "输入", "source_text": "检查设置"}).json()["id"]
        url = f"/api/test-cases/{case_id}/plans"
        for payload in [{}, {"planning_request_id": "invalid"}]:
            assert client.post(url, json=payload).status_code == 422
        assert client.get(f"/api/test-cases/{uuid4()}/planning/stream").status_code == 404
