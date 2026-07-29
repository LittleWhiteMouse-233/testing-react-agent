from __future__ import annotations

import time
import tempfile
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.device.test_fake import FakeDeviceController
from app.llm.test_fake import ScriptedLLMProvider
from app.config import Settings
from app.domain.models import ActionDecision, WaitAction
from app.main import create_app


class BoundaryDevice(FakeDeviceController):
    async def wait(self, duration_ms: int):
        await asyncio.sleep(0.1)
        return await super().wait(100)


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        checkpoint_path=tmp_path / "checkpoints.db",
        llm_mode="scripted",
    )
    return TestClient(create_app(settings))


def test_complete_scripted_run_and_exports() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, build_client(Path(directory)) as client:
        case = client.post(
            "/api/test-cases",
            json={
                "name": "Firmware version",
                "source_text": "打开设置并检查固件版本号正常显示",
            },
        ).json()
        revision = client.post(
            f"/api/test-cases/{case['id']}/plans", json={}
        ).json()
        assert {task["type"] for task in revision["plan"]["tasks"]} == {
            "act",
            "judge",
        }
        run_response = client.post(
            "/api/runs",
            json={
                "plan_revision_id": revision["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": revision["plan"]["assumptions"],
            },
        )
        assert run_response.status_code == 201
        run_id = run_response.json()["id"]
        for _ in range(100):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"finished", "cancelled"}:
                break
            time.sleep(0.05)
        assert run["overall_result"] == "PASS"
        assert [task["status"] for task in run["task_runs"]] == ["passed", "passed"]
        events = client.get(f"/api/runs/{run_id}/events").json()["items"]
        assert [item["sequence"] for item in events] == list(
            range(1, len(events) + 1)
        )
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["summary"]["passed"] == 2
        for export_format in ("json", "html"):
            export = client.post(
                f"/api/runs/{run_id}/exports", json={"format": export_format}
            )
            assert export.status_code == 201
            download = client.get(f"/api/artifacts/{export.json()['id']}")
            assert download.status_code == 200


def test_assumptions_and_single_run_guard() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, build_client(Path(directory)) as client:
        case = client.post(
            "/api/test-cases", json={"name": "A", "source_text": "B"}
        ).json()
        revision = client.post(
            f"/api/test-cases/{case['id']}/plans", json={}
        ).json()
        edited = revision["plan"]
        edited["assumptions"] = ["电视已登录"]
        manual = client.post(
            f"/api/plan-revisions/{revision['id']}/revisions",
            json={"plan": edited},
        ).json()
        rejected = client.post(
            "/api/runs",
            json={
                "plan_revision_id": manual["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        )
        assert rejected.status_code == 422


def test_cycle_guard_fail_fast_and_409_guard() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, build_client(Path(directory)) as client:
        case = client.post(
            "/api/test-cases", json={"name": "Cycle guard", "source_text": "持续等待"}
        ).json()
        revision = client.post(
            f"/api/test-cases/{case['id']}/plans", json={}
        ).json()
        plan = revision["plan"]
        plan["tasks"][0]["max_cycles"] = 1
        manual = client.post(
            f"/api/plan-revisions/{revision['id']}/revisions", json={"plan": plan}
        ).json()
        scripted = ScriptedLLMProvider(
            decisions=[
                ActionDecision(
                    type="action",
                    summary="Wait once",
                    action=WaitAction(type="WAIT", duration_ms=100),
                )
            ]
        )
        client.app.state.container.llm = scripted
        client.app.state.container.executor.llm = scripted
        first = client.post(
            "/api/runs",
            json={
                "plan_revision_id": manual["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        )
        assert first.status_code == 201
        conflict = client.post(
            "/api/runs",
            json={
                "plan_revision_id": manual["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        )
        assert conflict.status_code == 409
        run_id = first.json()["id"]
        for _ in range(100):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] == "finished":
                break
            time.sleep(0.03)
        assert run["overall_result"] == "FAIL"
        assert run["task_runs"][0]["status"] == "failed"
        assert run["task_runs"][0]["cycle_count"] == 1
        assert run["task_runs"][1]["status"] == "skipped"


def test_capture_failure_becomes_blocked() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, build_client(Path(directory)) as client:
        case = client.post(
            "/api/test-cases", json={"name": "Capture", "source_text": "截图失败"}
        ).json()
        revision = client.post(
            f"/api/test-cases/{case['id']}/plans", json={}
        ).json()
        client.app.state.container.devices["fake-tv"].capture_failures = 3
        run_id = client.post(
            "/api/runs",
            json={
                "plan_revision_id": revision["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        ).json()["id"]
        for _ in range(100):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] == "finished":
                break
            time.sleep(0.03)
        assert run["overall_result"] == "BLOCKED"
        assert run["task_runs"][0]["status"] == "blocked"
        assert run["task_runs"][1]["status"] == "skipped"


def test_cancel_is_applied_at_action_boundary() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, build_client(Path(directory)) as client:
        case = client.post(
            "/api/test-cases", json={"name": "Cancel", "source_text": "等待后取消"}
        ).json()
        revision = client.post(
            f"/api/test-cases/{case['id']}/plans", json={}
        ).json()
        scripted = ScriptedLLMProvider(
            decisions=[
                ActionDecision(
                    type="action",
                    summary="Wait for cancellation",
                    action=WaitAction(type="WAIT", duration_ms=10_000),
                )
            ]
        )
        client.app.state.container.llm = scripted
        client.app.state.container.executor.llm = scripted
        client.app.state.container.devices["fake-tv"] = BoundaryDevice()
        run_id = client.post(
            "/api/runs",
            json={
                "plan_revision_id": revision["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        ).json()["id"]
        for _ in range(100):
            events = client.get(f"/api/runs/{run_id}/events").json()["items"]
            if any(item["type"] == "action_started" for item in events):
                break
            time.sleep(0.02)
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.json()["cancel_requested"] is True
        for _ in range(100):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] == "cancelled":
                break
            time.sleep(0.03)
        assert run["overall_result"] == "CANCELLED"
        event_types = [
            item["type"]
            for item in client.get(f"/api/runs/{run_id}/events").json()["items"]
        ]
        assert "run_cancelled" in event_types
