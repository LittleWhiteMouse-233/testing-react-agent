"""Confirmed backward jumps, repeated execution history and failure precedence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage

from app.container import Container
from app.domain.activity import AgentActivity
from app.llm import ScriptedChatModelClient
from mcp_support import build_client, create_plan, finish_turn, start_run, tool_turn, wait_for_run


def terminal(status: str = "passed", memory: str | None = None) -> dict[str, Any]:
    return {"status": status, "summary": "Observed current node against confirmed criteria", "memory": memory}


def confirmed_plan(client: TestClient, task_types: list[str], targets: dict[int, int] | None = None) -> dict[str, Any]:
    plan = create_plan(client)
    tasks = [{"task_no": number, "type": task_type, "title": f"Task {number}",
              "goal": "Process the current node" if task_type != "goal" else "Check whether traversal is complete",
              "success_criteria": ["No pending nodes" if task_type == "goal" else "Current node meets the specification"],
              "jump_back_to_task_no": (targets or {}).get(number)}
             for number, task_type in enumerate(task_types, 1)]
    response = client.post(f"/api/test-plans/{plan['id']}/revisions", json={"content": {**plan["content"], "tasks": tasks}})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("failures,verdict", [(9, "PASS"), (10, "BLOCKED")])
def test_goal_tenth_failure_blocks_but_tenth_visit_can_pass(tmp_path: Path, failures: int, verdict: str) -> None:
    turns = [turn for _ in range(failures) for turn in [terminal(), terminal(), terminal("failed")]]
    if verdict == "PASS":
        turns.extend([terminal(), terminal(), terminal(), terminal()])
    with build_client(tmp_path, turns=turns) as client:
        plan = confirmed_plan(client, ["act", "judge", "goal", "judge"], {3: 1})
        detail = wait_for_run(client, start_run(client, plan))
        assert detail["run"]["verdict"] == verdict
        executions = detail["task_runs"]
        assert [entry["sequence"] for entry in executions] == list(range(1, len(executions) + 1))
        assert len({entry["id"] for entry in executions}) == len(executions)
        goals = [entry for entry in executions if entry["test_task_id"] == plan["content"]["tasks"][2]["test_task_id"]]
        assert len(goals) == 10
        assert [entry["status"] for entry in goals] == ["failed"] * failures + (["passed"] if verdict == "PASS" else [])
        assert "goal_max_failures" not in detail["environment"]
        if verdict == "BLOCKED":
            assert goals[-1]["result"]["reason_code"] == "goal_failure_limit"
            assert executions[-1]["status"] == "skipped"
        else:
            assert executions[-1]["status"] == "passed"


@pytest.mark.parametrize("last_judge_status,verdict", [("passed", "FAIL"), ("blocked", "BLOCKED")])
def test_judge_failure_continues_and_later_blocking_takes_precedence(
    tmp_path: Path, last_judge_status: str, verdict: str,
) -> None:
    turns = [terminal(), terminal("failed"), terminal(), terminal(last_judge_status)]
    if last_judge_status == "passed":
        turns.append(terminal())
    with build_client(tmp_path, turns=turns) as client:
        plan = confirmed_plan(client, ["act", "judge", "act", "judge", "act"])
        detail = wait_for_run(client, start_run(client, plan))
        assert detail["run"]["verdict"] == verdict
        assert [entry["status"] for entry in detail["task_runs"]] == [
            "passed", "failed", "passed", last_judge_status,
            "passed" if last_judge_status == "passed" else "skipped",
        ]


@pytest.mark.parametrize("later_failure,verdict", [(False, "PASS"), (True, "BLOCKED")])
def test_goals_count_failures_independently_and_pass_does_not_reset_them(
    tmp_path: Path, later_failure: bool, verdict: str,
) -> None:
    turns = [terminal(), terminal(), terminal("failed"), terminal(), terminal(),
             terminal("failed"), terminal(), terminal("failed" if later_failure else "passed")]
    if not later_failure:
        turns.append(terminal())
    with build_client(tmp_path, turns=turns, goal_max_failures=2) as client:
        plan = confirmed_plan(client, ["act", "goal", "goal", "goal"], {2: 1, 3: 2, 4: 2})
        detail = wait_for_run(client, start_run(client, plan))
        assert detail["run"]["verdict"] == verdict
        numbers = {task["test_task_id"]: task["definition"]["task_no"] for task in plan["content"]["tasks"]}
        started = [entry for entry in detail["task_runs"] if entry["started_at"] is not None]
        assert [numbers[entry["test_task_id"]] for entry in started] == [1, 2, 3, 2, 3, 4, 2, 3] + ([] if later_failure else [4])
        assert [entry["status"] for entry in started if numbers[entry["test_task_id"]] == 2] == ["passed"] * 3
        if later_failure:
            assert started[-1]["result"]["reason_code"] == "goal_failure_limit"


def test_reentered_memory_uses_actual_execution_numbers_and_resets_for_a_new_run(tmp_path: Path) -> None:
    turns = [terminal(), terminal("failed", "Node A has a defect"), terminal("failed", "Branch B remains"),
             terminal(memory="Branch B visited"), terminal(), terminal(), terminal()]
    with build_client(tmp_path, turns=turns * 2, goal_max_failures=2) as client:
        plan = confirmed_plan(client, ["act", "judge", "goal", "act"], {3: 1})
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        for _ in range(2):
            first_invocation = len(model.invocations)
            detail = wait_for_run(client, start_run(client, plan))
            assert detail["run"]["verdict"] == "FAIL"
            contexts = [json.loads(messages[1].text) for messages in model.invocations[first_invocation:]]
            memories = [json.loads(context["memory_text"]) for context in contexts]
            assert memories[0] == []
            assert memories[3] == [
                {"task_no": 2, "execution_no": 1, "memory": "Node A has a defect"},
                {"task_no": 3, "execution_no": 1, "memory": "Branch B remains"},
            ]
            assert memories[4] == [*memories[3], {"task_no": 1, "execution_no": 2, "memory": "Branch B visited"}]


def test_repeated_images_and_exports_keep_all_execution_origins(tmp_path: Path) -> None:
    turns = [tool_turn(1), terminal(memory="First node image"), terminal("failed"),
             tool_turn(2), terminal(memory="Second node image"), terminal()]
    with build_client(tmp_path, turns=turns) as client:
        plan = confirmed_plan(client, ["act", "goal"], {2: 1})
        run_id = start_run(client, plan)
        detail = wait_for_run(client, run_id)
        report = client.get(f"/api/runs/{run_id}").json()
        assert report["detail"] == detail
        images = report["artifacts"]
        assert len(images) == 2
        assert {image["task_run_id"] for image in images} == {detail["task_runs"][0]["id"], detail["task_runs"][2]["id"]}
        for export_format in ("json", "html"):
            exported = client.post(f"/api/runs/{run_id}/exports", json={"format": export_format})
            assert exported.status_code == 201, exported.text
            downloaded = client.get(f"/api/artifacts/{exported.json()['id']}")
            if export_format == "json":
                assert downloaded.json()["detail"] == detail
            else:
                assert "Goal 累计失败上限" not in downloaded.text
                assert "第 2 次执行" in downloaded.text
                assert "条件未满足" in downloaded.text
                assert "First node image" in downloaded.text and "Second node image" in downloaded.text


@pytest.mark.parametrize("change", [{"task_no": 3}, {"jump_back_to_task_no": None},
                                    {"jump_back_to_task_no": 2}, {"jump_back_to_task_no": 7}])
def test_revision_rejects_invalid_number_or_jump_without_repair(tmp_path: Path, change: dict[str, Any]) -> None:
    with build_client(tmp_path) as client:
        plan = confirmed_plan(client, ["act", "goal"], {2: 1})
        previous_versions = client.get(f"/api/test-cases/{plan['test_case_id']}/plans").json()["items"]
        content = {**plan["content"], "tasks": [task["definition"] for task in plan["content"]["tasks"]]}
        content["tasks"][1] = {**content["tasks"][1], **change}
        response = client.post(f"/api/test-plans/{plan['id']}/revisions", json={"content": content})
        assert response.status_code == 422, response.text
        versions = client.get(f"/api/test-cases/{plan['test_case_id']}/plans").json()["items"]
        assert versions[0] == plan
        assert versions == previous_versions


@pytest.mark.parametrize("model_error,verdict", [(False, "CANCELLED"), (True, "BLOCKED")])
def test_cancel_during_reentered_goal_preserves_failure_history_and_exception_priority(
    tmp_path: Path, model_error: bool, verdict: str,
) -> None:
    def cancel_during_goal(messages: list[BaseMessage]) -> AIMessage:
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        work = container.run_service._work
        assert work is not None
        work.cancellation.set()
        if model_error:
            raise RuntimeError("Model connection interrupted")
        return finish_turn(9, "failed")

    turns = [terminal(), terminal("failed"), terminal("failed"), terminal(), terminal(), cancel_during_goal]
    with build_client(tmp_path, turns=turns, execution_model_call_max_attempts=1) as client:
        plan = confirmed_plan(client, ["act", "judge", "goal", "act"], {3: 1})
        detail = wait_for_run(client, start_run(client, plan))
        assert detail["run"]["verdict"] == verdict
        executions = detail["task_runs"]
        assert [entry["status"] for entry in executions[:3]] == ["passed", "failed", "failed"]
        if model_error:
            assert [entry["status"] for entry in executions[-2:]] == ["blocked", "skipped"]
        else:
            assert len(executions) == 6
            assert executions[-1]["status"] == "cancelled"
            assert all(entry["status"] != "skipped" for entry in executions)
