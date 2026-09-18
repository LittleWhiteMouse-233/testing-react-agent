"""Mutable case intent and archive boundaries preserve immutable run history."""
from __future__ import annotations

from uuid import uuid4

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from app.container import Container
from app.domain.errors import PlanningFailure, TestCaseBusy as CaseBusy, TestCasePlanning as CasePlanning
from app.domain.planning import TestCaseContent as CaseContent
from app.llm import ScriptedChatModelClient
from app.persistence.db import Base
from mcp_support import build_client, create_plan, finish_turn, read_records, start_run, tool_turn, wait_for_run


def test_update_and_archive_preserve_all_plan_versions_runs_and_evidence(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[turn for index in range(3) for turn in (tool_turn(index * 2), finish_turn(index * 2 + 1))]) as client:
        container = cast(Container, cast(FastAPI, client.app).state.container)
        assert client.portal is not None
        first_plan = create_plan(client)
        case_id = first_plan["test_case_id"]
        case_url = f"/api/test-cases/{case_id}"
        original_case = client.get(case_url).json()

        async def wait_for_cleanup() -> None:
            work = container.run_service._work
            if work is not None and work.task is not None:
                await asyncio.wait_for(work.task, 15)

        run_ids = []
        for _ in range(2):
            run_ids.append(start_run(client, first_plan))
            wait_for_run(client, run_ids[-1])
            client.portal.call(wait_for_cleanup)
        original_report = client.get(f"/api/runs/{run_ids[0]}").json()
        updated_content = {"name": "新的用例名称", "source_text": "检查更新后的目标"}
        updated = client.patch(case_url, json=updated_content)
        assert updated.status_code == 200, updated.text
        assert updated.json() == {**original_case, "content": updated_content}
        assert client.get(f"/api/runs/{run_ids[0]}").json() == original_report
        assert client.get(case_url + "/plans").json()["items"][0] == first_plan

        revision_content = {**first_plan["content"], "tasks": [task["definition"] for task in first_plan["content"]["tasks"]]}
        second_plan = client.post(f"/api/test-plans/{first_plan['id']}/revisions", json={"content": revision_content}).json()
        assert second_plan["planning_context"] == first_plan["planning_context"]
        run_ids.append(start_run(client, second_plan))
        wait_for_run(client, run_ids[-1])
        client.portal.call(wait_for_cleanup)
        for action, payload in [("/api/runs", {"test_plan_id": first_plan["id"], "assumptions_confirmed": True}),
                                (f"/api/test-plans/{first_plan['id']}/revisions", {"content": revision_content})]:
            assert client.post(action, json=payload).status_code == 409
        replanned = client.post(case_url + "/plans", json={"planning_request_id": str(uuid4()), "user_input": "采用新的名称和描述"}).json()
        assert replanned["planning_context"]["test_case_content"] == updated_content

        listed = client.get("/api/runs", params={"test_case_id": case_id}).json()
        assert {run["id"] for run in listed["items"]} == set(run_ids)
        assert {run["test_plan_id"] for run in listed["items"]} == {first_plan["id"], second_plan["id"]}
        assert all(run["test_case_name"] == updated_content["name"] for run in listed["items"])
        stats = client.get("/api/runs/statistics", params={"test_case_id": case_id}).json()
        export = client.post(f"/api/runs/{run_ids[0]}/exports", json={"format": "json"}).json()
        export_content = client.get(f"/api/artifacts/{export['id']}").content
        report_before_archive = client.get(f"/api/runs/{run_ids[0]}").json()
        files = {path: path.read_bytes() for path in container.settings.artifacts_dir.rglob("*") if path.is_file()}
        checkpoint = container.settings.checkpoints.read_bytes()

        async def record_counts() -> dict[str, int]:
            async with container.sessions() as session:
                return {table.name: int(await session.scalar(select(func.count()).select_from(table)) or 0)
                        for table in Base.metadata.sorted_tables}

        counts = client.portal.call(record_counts)
        other_case = client.post("/api/test-cases", json={"name": "保留用例", "source_text": "无运行"}).json()
        counts["test_cases"] += 1
        assert client.post(case_url + "/archive").status_code == 204
        assert client.portal.call(record_counts) == counts
        assert all(path.read_bytes() == content for path, content in files.items())
        assert container.settings.checkpoints.read_bytes() == checkpoint
        assert client.get("/api/test-cases").json()["items"][0]["id"] == other_case["id"]
        assert client.get("/api/test-cases", params={"search": updated_content["name"], "limit": 1}).json() == {"items": [], "total": 0}
        for url in [case_url, case_url + "/plans"]:
            assert client.get(url).status_code == 404
        assert client.patch(case_url, json=updated_content).status_code == 404
        assert client.post(case_url + "/archive").status_code == 404
        assert client.post(case_url + "/plans", json={"planning_request_id": str(uuid4()), "user_input": "不能规划"}).status_code == 404
        assert client.post(f"/api/test-plans/{replanned['id']}/revisions", json={"content": revision_content}).status_code == 404
        mcp_before = read_records(tmp_path)
        assert client.post("/api/runs", json={"test_plan_id": replanned["id"], "assumptions_confirmed": True}).status_code == 404
        assert read_records(tmp_path) == mcp_before
        history_cases = client.get("/api/runs/test-cases", params={"search": updated_content["name"], "limit": 1}).json()
        assert history_cases == {"items": [{**updated.json(), "is_archived": True}], "total": 1}
        assert client.get("/api/runs/test-cases", params={"offset": 1}).json() == {"items": [], "total": 1}
        assert client.get("/api/runs/statistics", params={"test_case_id": case_id}).json() == stats
        assert all(run["test_case_archived"] for run in client.get("/api/runs", params={"test_case_id": case_id}).json()["items"])
        assert client.get(f"/api/runs/{run_ids[0]}").json() == {**report_before_archive, "test_case_archived": True}
        assert client.get(f"/api/runs/{run_ids[0]}/stream").status_code == 200
        assert client.get(f"/api/artifacts/{export['id']}").content == export_content
        for artifact in original_report["artifacts"]:
            assert client.get(f"/api/artifacts/{artifact['id']}").status_code == 200
        for export_format in ["json", "html"]:
            response = client.post(f"/api/runs/{run_ids[0]}/exports", json={"format": export_format})
            assert response.status_code == 201, response.text
            assert client.get(f"/api/artifacts/{response.json()['id']}").status_code == 200


@pytest.mark.parametrize("content", [
    {"name": "", "source_text": "正文"}, {"name": " \n ", "source_text": "正文"},
    {"name": "标题", "source_text": "\t "}, {"name": "长" * 201, "source_text": "正文"},
    {"source_text": "缺少名称"}, {"name": "缺少正文"},
])
def test_update_validates_both_fields_without_partial_writes(tmp_path: Path, content: dict) -> None:
    with build_client(tmp_path) as client:
        original = client.post("/api/test-cases", json={"name": "名称", "source_text": "正文"}).json()
        url = f"/api/test-cases/{original['id']}"
        assert client.patch(url, json=content).status_code == 422
        assert client.get(url).json() == original


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_planning_blocks_changes_until_request_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    with build_client(tmp_path) as client:
        container = cast(Container, cast(FastAPI, client.app).state.container)
        assert client.portal is not None
        case = client.post("/api/test-cases", json={"name": "名称", "source_text": "正文"}).json()
        other = client.post("/api/test-cases", json={"name": "其他用例", "source_text": "正文"}).json()
        if outcome == "failure":
            container.model_provider.replace_client_for_testing("default", ScriptedChatModelClient(plans=[{"title": "缺少任务"}, {"title": "缺少任务"}, {"title": "缺少任务"}]))
        original_generate = container.planning_graph.generate
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_generate(*args, **kwargs)

        monkeypatch.setattr(container.planning_graph, "generate", blocked_generate)

        async def exercise() -> None:
            first = asyncio.create_task(container.planning.generate(test_case_id=case["id"], planning_request_id=str(uuid4())))
            await asyncio.wait_for(entered.wait(), 5)
            # Rejecting another request must not release the first's occupation.
            with pytest.raises(CaseBusy):
                await container.planning.generate(test_case_id=case["id"], planning_request_id=str(uuid4()))
            with pytest.raises(CasePlanning):
                with container.test_case_lock.hold(case["id"], "editing"):
                    await container.repository.update_test_case(case["id"], CaseContent(name="修改", source_text="修改"))
            with pytest.raises(CasePlanning):
                with container.test_case_lock.hold(case["id"], "archiving"):
                    await container.repository.archive_test_case(case["id"])
            with container.test_case_lock.hold(other["id"], "archiving"):
                await container.repository.archive_test_case(other["id"])
            if outcome == "cancelled":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                release.set()
                if outcome == "failure":
                    with pytest.raises(PlanningFailure):
                        await first
                else:
                    await first
            with container.test_case_lock.hold(case["id"], "editing"):
                await container.repository.update_test_case(case["id"], CaseContent(name="修改", source_text="修改"))
            with container.test_case_lock.hold(case["id"], "archiving"):
                await container.repository.archive_test_case(case["id"])

        client.portal.call(exercise)


@pytest.mark.parametrize("operation", ["editing", "planning", "revising"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_plan_page_writes_exclude_other_writes_and_run_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, cancelled: bool,
) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        assert client.portal is not None
        case_url = f"/api/test-cases/{plan['test_case_id']}"
        original_plans = client.get(case_url + "/plans").json()["items"]
        other = client.post("/api/test-cases", json={"name": "其他", "source_text": "正文"}).json()
        revision_content = {**plan["content"], "tasks": [task["definition"] for task in plan["content"]["tasks"]]}
        requests = {
            "editing": ("PATCH", case_url, {"name": "修改", "source_text": "新正文"}),
            "planning": ("POST", case_url + "/plans", {"planning_request_id": str(uuid4()), "user_input": "调整计划"}),
            "revising": ("POST", f"/api/test-plans/{plan['id']}/revisions", {"content": revision_content}),
            "running": ("POST", "/api/runs", {"test_plan_id": plan["id"], "assumptions_confirmed": True}),
        }
        entered, release = asyncio.Event(), asyncio.Event()
        owner = container.planning_graph if operation == "planning" else container.repository
        method_name = {"editing": "update_test_case", "planning": "generate", "revising": "create_plan"}[operation]
        original = getattr(owner, method_name)

        async def delayed_write(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, method_name, delayed_write)

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                method, url, payload = requests[operation]
                pending = asyncio.create_task(api.request(method, url, json=payload))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    for attempted, (method, url, payload) in requests.items():
                        response = await api.request(method, url, json=payload)
                        assert response.status_code == 409, response.text
                        expected_code = "test_case_planning" if operation == "planning" and attempted == "editing" else "test_case_busy"
                        assert response.json()["code"] == expected_code
                    assert (await api.get(case_url + "/plans")).json()["items"] == original_plans
                    assert read_records(tmp_path) == []
                    assert (await api.get(f"/api/test-cases/{other['id']}")).status_code == 200
                    # Another case may still take a write occupation.
                    with container.test_case_lock.hold(other["id"], "editing"):
                        pass
                finally:
                    if cancelled:
                        pending.cancel()
                    else:
                        release.set()
                    if cancelled:
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                    else:
                        assert (await pending).status_code == (200 if operation == "editing" else 201)
                monkeypatch.setattr(owner, method_name, original)
                response = await api.patch(case_url, json={"name": "解除占用", "source_text": "可以保存"})
                assert response.status_code == 200, response.text

        client.portal.call(exercise)


def test_accepted_start_blocks_archive_during_mcp_preparation_and_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, cast(FastAPI, client.app).state.container)
        assert client.portal is not None
        entered, release = asyncio.Event(), asyncio.Event()
        executing, finish = asyncio.Event(), asyncio.Event()
        original_connect = container.tools.connect
        original_run = container.executor.run

        @asynccontextmanager
        async def connect(**kwargs):
            entered.set()
            await release.wait()
            async with original_connect(**kwargs) as tools:
                yield tools

        async def run(*args, **kwargs):
            executing.set()
            await finish.wait()
            return await original_run(*args, **kwargs)

        monkeypatch.setattr(container.tools, "connect", connect)
        monkeypatch.setattr(container.executor, "run", run)
        pending: asyncio.Task | None = None

        async def begin() -> None:
            nonlocal pending
            pending = asyncio.create_task(container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True))
            await asyncio.wait_for(entered.wait(), 5)

        async def prepare() -> str:
            release.set()
            assert pending is not None
            created = await asyncio.wait_for(pending, 15)
            await asyncio.wait_for(executing.wait(), 5)
            return created.id

        client.portal.call(begin)
        case_url = f"/api/test-cases/{plan['test_case_id']}"
        run_id = ""
        for stage in ["preparing", "pending"]:
            rejected = client.post(case_url + "/archive")
            assert rejected.status_code == 409
            assert rejected.json()["code"] == "test_case_has_active_run"
            assert client.patch(case_url, json={"name": "启动期间可修改", "source_text": "不影响运行快照"}).status_code == 200
            if stage == "preparing":
                run_id = client.portal.call(prepare)
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202

        async def finish_run() -> None:
            finish.set()
            work = container.run_service._work
            assert work is not None and work.task is not None
            await asyncio.wait_for(work.task, 15)

        client.portal.call(finish_run)
        assert client.get(f"/api/runs/{run_id}").json()["detail"]["run"]["verdict"] == "CANCELLED"
        assert client.post(case_url + "/archive").status_code == 204


def test_failed_mcp_start_releases_case_reservation(tmp_path: Path) -> None:
    # The real MCP provider rejects its missing config at its production boundary.
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, cast(FastAPI, client.app).state.container)
        container.settings.mcp_config_path.unlink()
        response = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert response.status_code == 502
        assert client.post(f"/api/test-cases/{plan['test_case_id']}/archive").status_code == 204


@pytest.mark.parametrize("complete_archive", [True, False])
def test_archive_reserves_case_before_database_work_and_releases_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete_archive: bool,
) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        assert client.portal is not None
        case_id = plan["test_case_id"]
        other = client.post("/api/test-cases", json={"name": "其他用例", "source_text": "正文"}).json()
        entered, release = asyncio.Event(), asyncio.Event()
        original_archive = container.repository.archive_test_case

        async def delayed_archive(test_case_id: str) -> None:
            entered.set()
            await release.wait()
            await original_archive(test_case_id)

        monkeypatch.setattr(container.repository, "archive_test_case", delayed_archive)

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                case_url = f"/api/test-cases/{case_id}"
                archiving = asyncio.create_task(api.post(case_url + "/archive"))
                await asyncio.wait_for(entered.wait(), 5)
                revision_content = {**plan["content"], "tasks": [task["definition"] for task in plan["content"]["tasks"]]}
                attempts = [
                    ("GET", case_url, None), ("GET", case_url + "/plans", None),
                    ("PATCH", case_url, {"name": "不能修改", "source_text": "归档占用中"}),
                    ("POST", case_url + "/plans", {"planning_request_id": str(uuid4()), "user_input": "不能规划"}),
                    ("POST", f"/api/test-plans/{plan['id']}/revisions", {"content": revision_content}),
                    ("POST", "/api/runs", {"test_plan_id": plan["id"], "assumptions_confirmed": True}),
                ]
                for method, url, payload in attempts:
                    response = await api.request(method, url, json=payload)
                    assert response.status_code == 409, response.text
                    assert response.json()["code"] == "test_case_busy"
                assert read_records(tmp_path) == []
                assert (await api.patch(f"/api/test-cases/{other['id']}", json={"name": "其他用例不受影响", "source_text": "可编辑"})).status_code == 200
                if complete_archive:
                    release.set()
                    assert (await archiving).status_code == 204
                else:
                    archiving.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await archiving
                container.test_case_lock.ensure_available(case_id, "editing")
                response = await api.patch(case_url, json={"name": "归档取消后可修改", "source_text": "内容"})
                assert response.status_code == (404 if complete_archive else 200)

        client.portal.call(exercise)
