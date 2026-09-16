from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event
from typing import cast

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import LLMProfileSettings
from app.container import Container
from app.domain.errors import PlanningFailure
from app.domain.planning import TestCaseContent as CaseContent
from app.llm import ChatModelClient, RealChatModelClient, ScriptedChatModelClient
from app.planning.graph import PlanDraft, PlanningGraph
from app.prompts import load_prompt
from mcp_support import build_client


def install_model(client: TestClient, model: ScriptedChatModelClient) -> Container:
    container = cast(Container, cast(FastAPI, client.app).state.container)
    container.model_provider.replace_client_for_testing("default", model)
    return container


@pytest.mark.parametrize("payload", [None, {}, {"user_input": None}, {"user_input": ""}, {"user_input": " \n "}, {"user_input": "补充检查要求"}])
def test_initial_planning_accepts_optional_input(tmp_path: Path, payload: dict | None) -> None:
    with build_client(tmp_path) as client:
        model = ScriptedChatModelClient()
        install_model(client, model)
        case_content = {"name": "原始用例", "source_text": "检查设置页面"}
        case = client.post("/api/test-cases", json=case_content).json()
        response = client.post(f"/api/test-cases/{case['id']}/plans", json=payload)
        assert response.status_code == 201, response.text
        plan = response.json()
        expected_input = (payload or {}).get("user_input")
        expected_input = expected_input.strip() or None if expected_input else None
        assert plan["planning_context"]["user_input"] == expected_input
        assert plan["origin"] == "planning"
        assert plan["derived_from_plan_id"] is None
        messages = model.invocations[0]
        assert [type(message) for message in messages] == [SystemMessage, HumanMessage, HumanMessage]
        assert json.loads(str(messages[1].content)) == case_content
        assert messages[2].content == (expected_input or "无额外要求。")


@pytest.mark.parametrize("payload", [None, {}, {"user_input": None}, {"user_input": ""}, {"user_input": " \n "}])
def test_replanning_rejects_missing_input_before_model_call(tmp_path: Path, payload: dict | None) -> None:
    with build_client(tmp_path) as client:
        model = ScriptedChatModelClient()
        install_model(client, model)
        case = client.post("/api/test-cases", json={"name": "用例", "source_text": "检查画面"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url).json()
        response = client.post(url, json=payload)
        assert response.status_code == 422
        assert response.json()["code"] == "planning_user_input_required"
        assert len(model.invocations) == 1
        assert client.get(url).json()["items"] == [first]


def test_replanning_uses_latest_revision_and_only_current_input(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        model = ScriptedChatModelClient()
        install_model(client, model)
        case_content = {"name": "原始用例", "source_text": "检查画面"}
        case = client.post("/api/test-cases", json=case_content).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url, json={"user_input": "第一轮输入"}).json()
        revision_content = {
            **first["content"],
            "title": "人工修订标题",
            "assumptions": ["人工准备的前置条件"],
            "setup_steps": ["人工补充提示"],
            "tasks": [task["definition"] for task in first["content"]["tasks"]],
        }
        revision_response = client.post(f"/api/test-plans/{first['id']}/revisions", json={"content": revision_content})
        assert revision_response.status_code == 201, revision_response.text
        revision = revision_response.json()
        assert revision["planning_context"] == first["planning_context"]
        second_response = client.post(url, json={"user_input": "第二轮修改要求"})
        assert second_response.status_code == 201, second_response.text
        second = second_response.json()
        messages = model.invocations[1]
        assert [type(message) for message in messages] == [SystemMessage, HumanMessage, AIMessage, HumanMessage]
        assert json.loads(str(messages[1].content)) == case_content
        assert json.loads(str(messages[2].content)) == revision_content
        assert messages[3].content == "第二轮修改要求"
        assert second["derived_from_plan_id"] == revision["id"]
        assert second["origin"] == "replanning"
        assert second["planning_context"]["user_input"] == "第二轮修改要求"
        assert not {task["test_task_id"] for task in revision["content"]["tasks"]} & {
            task["test_task_id"] for task in second["content"]["tasks"]
        }
        # Even JSON-shaped extra input must not replace the original case in the fake model.
        third_input = '{"name":"补充信息","source_text":"第三轮输入"}'
        third_response = client.post(url, json={"user_input": third_input})
        assert third_response.status_code == 201, third_response.text
        third = third_response.json()
        assert third["derived_from_plan_id"] == second["id"]
        assert third["content"]["tasks"][0]["definition"]["goal"] == case_content["source_text"]
        third_messages = model.invocations[2]
        assert len(third_messages) == 4
        assert third_messages[3].content == third_input
        assert "第一轮输入" not in str(third_messages)
        assert "第二轮修改要求" not in str(third_messages)
        assert json.loads(str(third_messages[2].content)) == {
            **second["content"], "tasks": [task["definition"] for task in second["content"]["tasks"]],
        }
        history = client.get(url).json()["items"]
        assert history == [third, second, revision, first]


@pytest.mark.parametrize("existing_plan", [False, True])
def test_generation_conflict_does_not_save_or_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_plan: bool) -> None:
    with build_client(tmp_path) as client:
        model = ScriptedChatModelClient()
        container = install_model(client, model)
        case = client.post("/api/test-cases", json={"name": "并发用例", "source_text": "检查画面"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url).json() if existing_plan else None
        generated = Event()
        release = Event()
        original_generate = container.planning_graph.generate

        async def delayed_generate(
            request: CaseContent, *, model_client: ChatModelClient,
            previous_plan: PlanDraft | None = None, user_input: str | None = None,
        ) -> PlanDraft:
            draft = await original_generate(request, model_client=model_client, previous_plan=previous_plan, user_input=user_input)
            if user_input == "等待中的修改":
                generated.set()
                assert await asyncio.to_thread(release.wait, 10)
            return draft

        monkeypatch.setattr(container.planning_graph, "generate", delayed_generate)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.post, url, json={"user_input": "等待中的修改"})
            try:
                assert generated.wait(10)
                if first:
                    content = {**first["content"], "tasks": [task["definition"] for task in first["content"]["tasks"]]}
                    winner_response = client.post(f"/api/test-plans/{first['id']}/revisions", json={"content": content})
                else:
                    winner_response = client.post(url)
                assert winner_response.status_code == 201, winner_response.text
            finally:
                release.set()
            rejected = pending.result(timeout=10)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "test_plan_not_latest"
        assert len(model.invocations) == 2
        assert client.get(url).json()["items"] == ([winner_response.json(), first] if first else [winner_response.json()])


def test_planning_retry_keeps_current_context_and_failure_creates_no_version(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        model = ScriptedChatModelClient()
        install_model(client, model)
        case = client.post("/api/test-cases", json={"name": "用例", "source_text": "检查画面"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url).json()
        invalid_model = ScriptedChatModelClient(plans=[{"title": "缺少任务"}, {"title": "缺少任务"}, {"title": "缺少任务"}])
        install_model(client, invalid_model)
        failed = client.post(url, json={"user_input": "细化任务"})
        assert failed.status_code == 502
        assert len(invalid_model.invocations) == 3
        for messages in invalid_model.invocations:
            assert messages[3].content == "细化任务"
            assert isinstance(messages[2], AIMessage)
        assert client.get(url).json()["items"] == [first]


@pytest.mark.parametrize("invalid_attempts", [0, 1, 3])
async def test_real_planning_json_mode_validates_responses(
    monkeypatch: pytest.MonkeyPatch, invalid_attempts: int,
) -> None:
    case = CaseContent(name="设置", source_text="进入设置并检查标题")
    expected_plan = await PlanningGraph(load_prompt("planner")).generate(
        case, model_client=ScriptedChatModelClient(),
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload
        system_prompt = payload["messages"][0]["content"]
        assert "JSON" in system_prompt
        assert '"success_criteria"' in system_prompt
        assert '"max_cycles"' in system_prompt
        content = (
            '{"title":"缺少任务"}'
            if len(requests) <= invalid_attempts
            else expected_plan.model_dump_json()
        )
        return httpx.Response(200, json={
            "id": "planning-response", "object": "chat.completion",
            "created": 0, "model": "deepseek-chat",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr(
            "app.llm.client.ChatOpenAI",
            partial(ChatOpenAI, http_async_client=http_client),
        )
        model = RealChatModelClient(LLMProfileSettings(
            id="planner", mode="real", model="deepseek-chat",
            base_url="https://model.invalid/v1", api_key="test-key",
        ))
        graph = PlanningGraph(load_prompt("planner"))
        if invalid_attempts == 3:
            with pytest.raises(PlanningFailure, match="Planning failed after 3 attempts"):
                await graph.generate(case, model_client=model)
        else:
            assert await graph.generate(case, model_client=model) == expected_plan
    assert len(requests) == min(invalid_attempts + 1, 3)
