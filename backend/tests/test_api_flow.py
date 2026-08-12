from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import Settings
from app.main import create_app


def build_client(root: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                data_dir=root,
                database_url=f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}",
                checkpoint_path=root / "checkpoints.db",
            )
        )
    )


def wait_for_run(client: TestClient, test_run_id: str) -> dict:
    detail: dict = {}
    for _ in range(200):
        response = client.get(f"/api/runs/{test_run_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["run"]["status"] == "finished":
            return detail
        time.sleep(0.03)
    raise AssertionError(f"Run did not finish: {detail}")


async def load_checkpoint_messages(
    checkpoint_path: Path, task_run_ids: list[str]
) -> dict[str, list[BaseMessage]]:
    result: dict[str, list[BaseMessage]] = {}
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        for task_run_id in task_run_ids:
            config: RunnableConfig = {
                "configurable": {"thread_id": f"task-run:{task_run_id}"}
            }
            checkpoint = await saver.aget(config)
            assert checkpoint is not None
            messages = checkpoint["channel_values"]["messages"]
            result[task_run_id] = cast(list[BaseMessage], messages)
    return result


def test_complete_message_first_run_and_exports() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            case_response = client.post(
                "/api/test-cases",
                json={"name": "Firmware", "source_text": "检查固件版本正常显示"},
            )
            assert case_response.status_code == 201, case_response.text
            test_case = case_response.json()
            assert test_case["content"]["name"] == "Firmware"
            assert "updated_at" not in test_case

            plan_response = client.post(
                f"/api/test-cases/{test_case['id']}/plans",
                json={"device_id": "fake-tv"},
            )
            assert plan_response.status_code == 201, plan_response.text
            plan = plan_response.json()
            assert [item["definition"]["type"] for item in plan["content"]["tasks"]] == [
                "act",
                "judge",
            ]

            run_response = client.post(
                "/api/runs",
                json={
                    "test_plan_id": plan["id"],
                    "device_id": "fake-tv",
                    "assumptions_confirmed": True,
                },
            )
            assert run_response.status_code == 201, run_response.text
            pending = run_response.json()
            assert pending["verdict"] is None
            assert pending["started_at"] is None
            assert pending["finished_at"] is None
            detail = wait_for_run(client, run_response.json()["id"])
            diagnostic_events = client.get(
                f"/api/runs/{detail['run']['id']}/events"
            ).json()
            assert detail["run"]["verdict"] == "PASS", diagnostic_events
            assert [item["status"] for item in detail["task_runs"]] == [
                "passed",
                "passed",
            ]
            assert detail["snapshot"]["execution_protocol_version"] == "1"
            assert "planning_model" not in detail["snapshot"]

            events_response = client.get(
                f"/api/runs/{detail['run']['id']}/events"
            )
            assert events_response.status_code == 200, events_response.text
            events = events_response.json()["items"]
            assert [item["sequence"] for item in events] == list(
                range(1, len(events) + 1)
            )
            event_types = [item["event"]["type"] for item in events]
            assert "message.appended" in event_types
            assert "observation.captured" not in event_types
            assert "tool.finished" not in event_types
            serialized_events = events_response.text
            assert "base64" not in serialized_events
            assert "image_artifact" in serialized_events
            message_ids = [
                item["event"]["message"]["message_id"]
                for item in events
                if item["event"]["type"] == "message.appended"
            ]
            assert len(message_ids) == len(set(message_ids))

            with client.stream(
                "GET", f"/api/runs/{detail['run']['id']}/stream?after=0"
            ) as stream:
                assert stream.status_code == 200
                assert stream.headers["content-type"].startswith(
                    "text/event-stream"
                )
                lines = list(stream.iter_lines())
            assert lines[0] == "retry: 1000"
            sse_ids = [
                int(line.removeprefix("id: "))
                for line in lines
                if line.startswith("id: ")
            ]
            sse_events = [
                json.loads(line.removeprefix("data: "))
                for line in lines
                if line.startswith("data: ")
            ]
            assert not any(line.startswith("event:") for line in lines)
            assert sse_ids == [item["sequence"] for item in events]
            assert sse_events == events

            header_cursor = events[-3]["sequence"]
            with client.stream(
                "GET",
                f"/api/runs/{detail['run']['id']}/stream?after=0",
                headers={"Last-Event-ID": str(header_cursor)},
            ) as resumed_stream:
                resumed_lines = list(resumed_stream.iter_lines())
            resumed_ids = [
                int(line.removeprefix("id: "))
                for line in resumed_lines
                if line.startswith("id: ")
            ]
            assert resumed_ids == [item["sequence"] for item in events[-2:]]

            query_cursor = events[-2]["sequence"]
            with client.stream(
                "GET",
                f"/api/runs/{detail['run']['id']}/stream?after={query_cursor}",
                headers={"Last-Event-ID": "0"},
            ) as max_cursor_stream:
                max_cursor_lines = list(max_cursor_stream.iter_lines())
            max_cursor_ids = [
                int(line.removeprefix("id: "))
                for line in max_cursor_lines
                if line.startswith("id: ")
            ]
            assert max_cursor_ids == [events[-1]["sequence"]]

            invalid_cursor = client.get(
                f"/api/runs/{detail['run']['id']}/stream",
                headers={"Last-Event-ID": "-1"},
            )
            assert invalid_cursor.status_code == 422
            assert invalid_cursor.json()["code"] == "validation_error"

            checkpoint_messages = asyncio.run(
                load_checkpoint_messages(
                    Path(directory) / "checkpoints.db",
                    [item["id"] for item in detail["task_runs"]],
                )
            )
            for task_run in detail["task_runs"]:
                persisted = [
                    item["event"]["message"]["message_id"]
                    for item in events
                    if item["event"]["type"] == "message.appended"
                    and item["event"]["task_run_id"] == task_run["id"]
                ]
                state_messages = checkpoint_messages[task_run["id"]]
                assert [message.id for message in state_messages] == persisted
                assert any(
                    block.get("type") == "image" and bool(block.get("base64"))
                    for message in state_messages
                    for block in message.content_blocks
                )

            report_response = client.get(
                f"/api/runs/{detail['run']['id']}/report"
            )
            assert report_response.status_code == 200, report_response.text
            report = report_response.json()
            assert report["detail"] == detail
            assert report["events"] == events
            assert len(report["artifacts"]) == 2
            assert "summary" not in report

            screenshot_download = client.get(
                f"/api/artifacts/{report['artifacts'][0]['id']}"
            )
            assert screenshot_download.status_code == 200
            assert screenshot_download.headers["content-type"] == "image/png"

            for export_format in ("json", "html"):
                export = client.post(
                    f"/api/runs/{detail['run']['id']}/exports",
                    json={"format": export_format},
                )
                assert export.status_code == 201, export.text
                artifact = export.json()
                assert artifact["type"] == f"{export_format}_export"
                download = client.get(f"/api/artifacts/{artifact['id']}")
                assert download.status_code == 200
                if export_format == "json":
                    assert download.headers["content-type"] == "application/json"
                    assert download.json() == report
                else:
                    assert download.headers["content-type"].startswith("text/html")
                    assert "data:image/png;base64," in download.text


def test_only_latest_plan_can_start() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            case = client.post(
                "/api/test-cases", json={"name": "Case", "source_text": "目标"}
            ).json()
            first = client.post(
                f"/api/test-cases/{case['id']}/plans",
                json={"device_id": "fake-tv"},
            ).json()
            second = client.post(
                f"/api/test-cases/{case['id']}/plans",
                json={"device_id": "fake-tv"},
            ).json()
            assert second["origin"] == "replanning"
            stale_revision = client.post(
                f"/api/test-plans/{first['id']}/revisions",
                json={
                    "content": {
                        "title": first["content"]["title"],
                        "setup_steps": first["content"]["setup_steps"],
                        "assumptions": first["content"]["assumptions"],
                        "tasks": [
                            item["definition"] for item in first["content"]["tasks"]
                        ],
                    }
                },
            )
            assert stale_revision.status_code == 409
            assert stale_revision.json()["code"] == "test_plan_not_latest"
            response = client.post(
                "/api/runs",
                json={
                    "test_plan_id": first["id"],
                    "device_id": "fake-tv",
                    "assumptions_confirmed": True,
                },
            )
            assert response.status_code == 409
            assert response.json()["code"] == "test_plan_not_startable"


def test_api_errors_use_the_stable_error_contract() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            invalid = client.post(
                "/api/test-cases", json={"name": "", "source_text": ""}
            )
            assert invalid.status_code == 422
            assert set(invalid.json()) == {"code", "message"}
            assert invalid.json()["code"] == "validation_error"

            missing = client.get(
                "/api/test-cases/11111111-1111-4111-8111-111111111111"
            )
            assert missing.status_code == 404
            assert set(missing.json()) == {"code", "message"}
            assert missing.json()["code"] == "test_case_not_found"
