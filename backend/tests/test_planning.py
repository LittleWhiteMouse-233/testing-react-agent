from __future__ import annotations

from mcp_support import capability_selection

from config_support import install_model_client

from uuid import uuid4

import asyncio
import base64
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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from app.container import Container
from app.domain.activity import AgentActivity
from app.domain.errors import PlanningFailure
from app.domain.planning import TestCaseContent as CaseContent, TestPlanPlanningContext as PlanningContext
from app.domain.resources import CapabilityCatalog, CapabilityColumnSnapshot, ResourceCatalogSnapshot, ToolCatalogSnapshot
from app.llm import ChatModelClient, RealChatModelClient, ScriptedChatModelClient
from app.planning.graph import PlanDraft, PlanningGraph
from app.resources.prompts import PromptCatalog
from mcp_support import PNG_BASE64, build_client, read_records
from config_support import build_model_profile, build_planning_settings


def install_model(client: TestClient, model: ScriptedChatModelClient, monkeypatch: pytest.MonkeyPatch) -> Container:
    container = cast(Container, cast(FastAPI, client.app).state.container)
    install_model_client(monkeypatch, model)
    return container


def planning_turn(index: int, name: str, arguments: dict) -> AIMessage:
    return AIMessage(content="Read references and design observable tasks", tool_calls=[{
        "name": name, "args": arguments, "id": f"planning-{index}", "type": "tool_call",
    }])


def expected_draft() -> PlanDraft:
    return PlanDraft.model_validate({
        "title": "设置标题", "setup_steps": [], "assumptions": [],
        "tasks": [{"task_no": 1, "type": "judge", "title": "验证标题", "goal": "观察设置页标题",
                   "success_criteria": ["标题完整显示且可读"]}],
    })


def graph_context(**planning_parameters: object) -> PlanningContext:
    return PlanningContext(
        test_case_content=CaseContent(name="设置", source_text="进入设置并检查标题"),
        planning_model=build_model_profile(), planning_prompt_fingerprint=None,
        planning_setting=build_planning_settings(**planning_parameters),
        capability_catalog=CapabilityCatalog[CapabilityColumnSnapshot](
            tool_catalog=ToolCatalogSnapshot(tools=[]), resource_catalog=ResourceCatalogSnapshot(), columns=[],
        ),
    )


def test_planning_empty_selection_freezes_its_own_settings(tmp_path: Path) -> None:
    with build_client(tmp_path, planning={"max_cycles": 7}) as client:
        case = client.post("/api/test-cases", json={"name": "规划", "source_text": "检查设置标题"}).json()
        response = client.post(f"/api/test-cases/{case['id']}/plans", json={
            "planning_request_id": str(uuid4()), "selected_columns": [],
        })
        assert response.status_code == 201, response.text
        context = response.json()["planning_context"]
        assert context["planning_setting"]["max_cycles"] == 7
        assert context["planning_setting"]["model_call_max_attempts"] == 3
        assert context["capability_catalog"]["columns"] == [{"id": "local:other", "selected": False}]
        assert all(tool["source_id"].startswith("builtin:") for tool in context["capability_catalog"]["tool_catalog"]["tools"])
        assert {column["id"] for column in client.get("/api/capabilities").json()["columns"]} == {"mcp:fixture", "local:other"}
        assert "capability_discovery_timeout_seconds" not in context
        assert client.get("/api/runs").json()["total"] == 0


@pytest.mark.parametrize("payload", [None, {}, {"user_input": None}, {"user_input": ""}, {"user_input": " \n "}, {"user_input": "补充检查要求"}])
def test_initial_planning_accepts_optional_input(tmp_path: Path, payload: dict | None) -> None:
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as model_patch:
        model = ScriptedChatModelClient(build_model_profile())
        install_model(client, model, model_patch)
        case_content = {"name": "原始用例", "source_text": "检查设置页面"}
        case = client.post("/api/test-cases", json=case_content).json()
        response = client.post(f"/api/test-cases/{case['id']}/plans", json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), **(payload or {})})
        assert response.status_code == 201, response.text
        plan = response.json()
        expected_input = (payload or {}).get("user_input")
        expected_input = expected_input.strip() or None if expected_input else None
        assert plan["planning_context"]["user_input"] == expected_input
        assert plan["origin"] == "planning"
        assert plan["derived_from_plan_id"] is None
        messages = model.invocations[0]
        assert [type(message) for message in messages] == [SystemMessage, HumanMessage]
        initial_input = json.loads(str(messages[1].content))
        assert initial_input["test_case_content"] == case_content
        assert initial_input["user_input"] == expected_input
        assert "max_cycles" not in initial_input and "planning_setting" not in initial_input


@pytest.mark.parametrize("payload", [None, {}, {"user_input": None}, {"user_input": ""}, {"user_input": " \n "}])
def test_replanning_rejects_missing_input_before_model_call(tmp_path: Path, payload: dict | None) -> None:
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as model_patch:
        model = ScriptedChatModelClient(build_model_profile())
        install_model(client, model, model_patch)
        case = client.post("/api/test-cases", json={"name": "用例", "source_text": "检查画面"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4())}).json()
        response = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), **(payload or {})})
        assert response.status_code == 422
        assert response.json()["code"] == "planning_user_input_required"
        assert len(model.invocations) == 1
        assert client.get(url).json()["items"] == [first]


def test_replanning_uses_latest_revision_and_only_current_input(tmp_path: Path) -> None:
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as model_patch:
        model = ScriptedChatModelClient(build_model_profile())
        install_model(client, model, model_patch)
        case_content = {"name": "原始用例", "source_text": "检查画面"}
        case = client.post("/api/test-cases", json=case_content).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), "user_input": "第一轮输入"}).json()
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
        second_response = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), "user_input": "第二轮修改要求"})
        assert second_response.status_code == 201, second_response.text
        second = second_response.json()
        messages = model.invocations[1]
        assert [type(message) for message in messages] == [SystemMessage, HumanMessage]
        initial_input = json.loads(str(messages[1].content))
        assert initial_input["test_case_content"] == case_content
        assert initial_input["previous_plan"] == revision_content
        assert initial_input["user_input"] == "第二轮修改要求"
        assert second["derived_from_plan_id"] == revision["id"]
        assert second["origin"] == "replanning"
        assert second["planning_context"]["user_input"] == "第二轮修改要求"
        assert not {task["test_task_id"] for task in revision["content"]["tasks"]} & {
            task["test_task_id"] for task in second["content"]["tasks"]
        }
        # Even JSON-shaped extra input must not replace the original case in the fake model.
        third_input = '{"name":"补充信息","source_text":"第三轮输入"}'
        third_response = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), "user_input": third_input})
        assert third_response.status_code == 201, third_response.text
        third = third_response.json()
        assert third["derived_from_plan_id"] == second["id"]
        assert third["content"]["tasks"][0]["definition"]["goal"] == case_content["source_text"]
        third_messages = model.invocations[2]
        assert len(third_messages) == 2
        third_context = json.loads(str(third_messages[1].content))
        assert third_context["user_input"] == third_input
        assert "第一轮输入" not in str(third_messages)
        assert "第二轮修改要求" not in str(third_messages)
        assert third_context["previous_plan"] == {
            **second["content"], "tasks": [task["definition"] for task in second["content"]["tasks"]],
        }
        history = client.get(url).json()["items"]
        assert history == [third, second, revision, first]


@pytest.mark.parametrize("existing_plan", [False, True])
def test_generation_rejects_concurrent_plan_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_plan: bool) -> None:
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as model_patch:
        model = ScriptedChatModelClient(build_model_profile())
        container = install_model(client, model, model_patch)
        case = client.post("/api/test-cases", json={"name": "并发用例", "source_text": "检查画面"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4())}).json() if existing_plan else None
        generated = Event()
        release = Event()
        original_generate = container.planning_graph.generate

        async def delayed_generate(
            request: PlanningContext, *, model_client: ChatModelClient, resource_tools: tuple[BaseTool, ...],
            previous_plan: PlanDraft | None = None,
            on_progress=None,
        ) -> PlanDraft:
            draft = await original_generate(request, model_client=model_client, resource_tools=resource_tools, previous_plan=previous_plan, on_progress=on_progress)
            if request.user_input == "等待中的修改":
                generated.set()
                assert await asyncio.to_thread(release.wait, 10)
            return draft

        monkeypatch.setattr(container.planning_graph, "generate", delayed_generate)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.post, url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), "user_input": "等待中的修改"})
            try:
                assert generated.wait(10)
                if first:
                    content = {**first["content"], "tasks": [task["definition"] for task in first["content"]["tasks"]]}
                    rejected = client.post(f"/api/test-plans/{first['id']}/revisions", json={"content": content})
                else:
                    rejected = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4())})
                assert rejected.status_code == 409, rejected.text
                assert rejected.json()["code"] == "test_case_busy"
            finally:
                release.set()
            completed = pending.result(timeout=10)
        assert completed.status_code == 201, completed.text
        assert len(model.invocations) == (2 if first else 1)
        assert client.get(url).json()["items"] == ([completed.json(), first] if first else [completed.json()])


def test_planning_retry_keeps_current_context_and_failure_creates_no_version(tmp_path: Path) -> None:
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as model_patch:
        model = ScriptedChatModelClient(build_model_profile())
        install_model(client, model, model_patch)
        case = client.post("/api/test-cases", json={"name": "用例", "source_text": "检查画面"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4())}).json()
        invalid_model = ScriptedChatModelClient(build_model_profile(), planning_turns=[
            planning_turn(index, "finish_plan", {"title": "缺少任务"}) for index in range(3)
        ])
        install_model(client, invalid_model, model_patch)
        failed = client.post(url, json={"selected_columns": capability_selection(client), "planning_request_id": str(uuid4()), "user_input": "细化任务"})
        assert failed.status_code == 502
        assert len(invalid_model.invocations) == 3
        for messages in invalid_model.invocations:
            assert json.loads(str(messages[1].content))["user_input"] == "细化任务"
        assert client.get(url).json()["items"] == [first]


async def planning_resource_tools(root: Path) -> tuple[BaseTool, ...]:
    from app.resources.static_resources import SkillResourceProvider
    from mcp_support import write_mcp_config, load_mcp_client_provider

    clients = await load_mcp_client_provider(write_mcp_config(root, [], servers=()))
    return SkillResourceProvider(root / "skills", clients).build_resource_tools()


@pytest.mark.parametrize("invalid_attempts", [0, 1, 3])
@pytest.mark.parametrize("correction_limit", [2, 3])
async def test_real_planning_tool_calling_validates_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_attempts: int, correction_limit: int,
) -> None:
    expected_plan = expected_draft()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert "response_format" not in payload
        assert {entry["function"]["name"] for entry in payload["tools"]} == {
            "finish_plan", "load_skill", "list_resources", "read_resource",
        }
        initial_input = json.loads(payload["messages"][1]["content"])
        assert "max_cycles" not in initial_input and "planning_setting" not in initial_input
        arguments = {"title": "缺少任务"} if len(requests) <= invalid_attempts else expected_plan.model_dump(mode="json")
        return httpx.Response(200, json={
            "id": "planning-response", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": f"finish-{len(requests)}", "type": "function",
                    "function": {"name": "finish_plan", "arguments": json.dumps(arguments)},
                }],
            }}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr("app.llm.client.ChatOpenAI", partial(ChatOpenAI, http_async_client=http_client))
        profile = build_model_profile(id="planner", mode="real", model="test", base_url="https://model.invalid/v1", api_key="test-key")
        model = RealChatModelClient(profile)
        context = graph_context(model_response_max_attempts=correction_limit).model_copy(update={"planning_model": profile})
        graph = PlanningGraph(PromptCatalog().prompt_for_activity(AgentActivity.PLANNING))
        tools = await planning_resource_tools(tmp_path)
        if invalid_attempts >= correction_limit:
            with pytest.raises(PlanningFailure, match="纠错额度耗尽"):
                await graph.generate(context, model_client=model, resource_tools=tools)
        else:
            assert await graph.generate(context, model_client=model, resource_tools=tools) == expected_plan
    assert len(requests) == min(invalid_attempts + 1, correction_limit)


@pytest.mark.parametrize("malformed_json", [True, False])
async def test_malformed_planning_call_is_corrected_with_valid_message_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed_json: bool,
) -> None:
    request_count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        payload = json.loads(request.content)
        if request_count == 2:
            # Providers reject an unresolved malformed call in the next request.
            assert all(not message.get("tool_calls") for message in payload["messages"])
            assert payload["messages"][-1]["role"] == "user"
        arguments = "{" if request_count == 1 and malformed_json else expected_draft().model_dump_json()
        call_id = "" if request_count == 1 and not malformed_json else f"finish-{request_count}"
        return httpx.Response(200, json={
            "id": "response", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function", "function": {"name": "finish_plan", "arguments": arguments},
                }],
            }}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr("app.llm.client.ChatOpenAI", partial(ChatOpenAI, http_async_client=http_client))
        profile = build_model_profile(mode="real", model="test", base_url="https://model.invalid/v1", api_key="test-key")
        result = await PlanningGraph(None).generate(
            graph_context().model_copy(update={"planning_model": profile}), model_client=RealChatModelClient(profile),
            resource_tools=await planning_resource_tools(tmp_path),
        )
    assert result == expected_draft() and request_count == 2


@pytest.mark.parametrize("failure", ["timeout", "provider"])
@pytest.mark.parametrize("transport_attempts", [1, 3])
async def test_planning_model_retries_remain_in_one_cycle(
    monkeypatch: pytest.MonkeyPatch, failure: str, transport_attempts: int,
) -> None:
    from app.planning.graph import PlanningProgress

    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(503, json={"error": {"message": "provider unavailable"}})

    progress: list[PlanningProgress] = []

    async def observe(event: PlanningProgress) -> None:
        progress.append(event)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr("app.llm.client.ChatOpenAI", partial(ChatOpenAI, http_async_client=http_client))
        profile = build_model_profile(id="planner", mode="real", model="test", base_url="https://model.invalid/v1",
                                      api_key="test-key", timeout_seconds=0.1)
        with pytest.raises(PlanningFailure, match="模型调用超时" if failure == "timeout" else "503"):
            await PlanningGraph(None).generate(graph_context(model_call_max_attempts=transport_attempts).model_copy(update={"planning_model": profile}),
                                               model_client=RealChatModelClient(profile), resource_tools=(), on_progress=observe)
    assert len(requests) == transport_attempts
    assert [event["cycle_count"] for event in progress if event["type"] == "planning.cycle_started"] == [1]


@pytest.mark.parametrize("limit, reads, succeeds", [(20, 19, True), (2, 2, False), (4, 3, True)])
async def test_planning_uses_configured_limit_and_can_finish_on_last_cycle(
    tmp_path: Path, limit: int, reads: int, succeeds: bool,
) -> None:
    model = ScriptedChatModelClient(build_model_profile(), planning_turns=[
        *[planning_turn(index, "list_resources", {}) for index in range(reads)],
        planning_turn(reads, "finish_plan", expected_draft().model_dump(mode="json")),
    ])
    context = graph_context(max_cycles=limit)
    tools = await planning_resource_tools(tmp_path)
    if succeeds:
        assert await PlanningGraph(None).generate(context, model_client=model, resource_tools=tools) == expected_draft()
    else:
        with pytest.raises(PlanningFailure, match="轮次耗尽"):
            await PlanningGraph(None).generate(context, model_client=model, resource_tools=tools)
    assert len(model.invocations) == limit


async def test_successful_resource_read_resets_consecutive_corrections(tmp_path: Path) -> None:
    model = ScriptedChatModelClient(build_model_profile(), planning_turns=[
        AIMessage(content="missing call"), AIMessage(content="missing call"),
        planning_turn(2, "list_resources", {}),
        AIMessage(content="missing call"), AIMessage(content="missing call"),
        planning_turn(5, "finish_plan", expected_draft().model_dump(mode="json")),
    ])
    result = await PlanningGraph(None).generate(graph_context(), model_client=model, resource_tools=await planning_resource_tools(tmp_path))
    assert result == expected_draft() and len(model.invocations) == 6


async def test_resource_timeout_is_reported_without_replaying_the_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = await planning_resource_tools(tmp_path)
    invocation_count = 0
    cancelled = asyncio.Event()

    async def stalled_read(**_arguments):
        nonlocal invocation_count
        invocation_count += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(next(available_tool for available_tool in tools if available_tool.name == "list_resources"), "coroutine", stalled_read)
    model = ScriptedChatModelClient(build_model_profile(), planning_turns=[
        planning_turn(1, "list_resources", {}),
        planning_turn(2, "finish_plan", expected_draft().model_dump(mode="json")),
    ])
    progress: list[dict] = []

    async def record_progress(event) -> None:
        progress.append(event)

    result = await PlanningGraph(None).generate(
        graph_context(tool_call_timeout_seconds=0.02), model_client=model, resource_tools=tools,
        on_progress=record_progress,
    )
    assert result == expected_draft() and invocation_count == 1 and cancelled.is_set()
    completed = [event for event in progress if event["type"] == "planning.tool_completed"]
    assert [event["status"] for event in completed] == ["error", "success"]
    assert "timed out" in completed[0]["reason"]
    assert "timed out" in str(model.invocations[1])


def test_planning_reads_skills_and_resources_without_executing_mcp_tools(tmp_path: Path) -> None:
    from app.resources import tools as tool_resources

    async def reject_assembly(*arguments: object) -> None:
        raise AssertionError("Planning must not assemble external execution tools")

    skill_root = tmp_path / "skills" / "display"
    skill_root.mkdir(parents=True)
    skill_entry = skill_root / "SKILL.md"
    skill_entry.write_text("---\nname: Display\ndescription: Display planning references\n---\nUNIQUE-SKILL-BODY\nRead rules.md.", encoding="utf-8")
    (skill_root / "rules.md").write_text("UNIQUE-REFERENCE-BODY: require an observable readable title.", encoding="utf-8")
    (skill_root / "reference.png").write_bytes(base64.b64decode(PNG_BASE64))
    model = ScriptedChatModelClient(build_model_profile(), planning_turns=[
        planning_turn(1, "load_skill", {"source_id": "local", "uri": skill_entry.as_uri()}),
        planning_turn(2, "read_resource", {"source_id": "local", "uri": "rules.md", "base_uri": skill_entry.as_uri()}),
        planning_turn(3, "list_resources", {"source_id": "local"}),
        planning_turn(4, "read_resource", {"source_id": "local", "uri": "reference.png", "base_uri": skill_entry.as_uri()}),
        planning_turn(5, "finish_plan", expected_draft().model_dump(mode="json")),
    ])
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as patch:
        patch.setattr(tool_resources, "as_langchain_tool", reject_assembly)
        install_model(client, model, patch)
        case = client.post("/api/test-cases", json={"name": "资源规划", "source_text": "检查标题"}).json()
        response = client.post(f"/api/test-cases/{case['id']}/plans", json={
            "planning_request_id": str(uuid4()), "selected_columns": capability_selection(client),
        })
        assert response.status_code == 201, response.text
        assert len(model.invocations) == 5
        initial = json.loads(str(model.invocations[0][1].content))
        assert {entry["name"] for entry in initial["execution_tools"]} == {"fixture_inspect"}
        assert "UNIQUE-SKILL-BODY" not in str(model.invocations[0])
        assert "UNIQUE-SKILL-BODY" in str(model.invocations[-1])
        assert "UNIQUE-REFERENCE-BODY" in str(model.invocations[-1])
        assert any(block["type"] == "image" and block.get("base64") == PNG_BASE64
                   for message in model.invocations[-1] for block in message.content_blocks)
        assert "UNIQUE-REFERENCE-BODY" not in response.text and PNG_BASE64 not in response.text
        assert not any(record["phase"] == "started" for record in read_records(tmp_path))
        assert client.get("/api/runs").json()["total"] == 0


def test_planning_denies_unselected_resource_and_all_mcp_tool_calls(tmp_path: Path) -> None:
    resource = tmp_path / "skills" / "private.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("PRIVATE-UNSELECTED-CONTENT", encoding="utf-8")
    model = ScriptedChatModelClient(build_model_profile(), planning_turns=[
        planning_turn(1, "fixture_inspect", {}),
        planning_turn(2, "read_resource", {"source_id": "local", "uri": resource.as_uri()}),
        planning_turn(3, "finish_plan", expected_draft().model_dump(mode="json")),
    ])
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as patch:
        install_model(client, model, patch)
        case = client.post("/api/test-cases", json={"name": "权限", "source_text": "检查标题"}).json()
        response = client.post(f"/api/test-cases/{case['id']}/plans", json={"planning_request_id": str(uuid4()), "selected_columns": []})
        assert response.status_code == 201, response.text
        initial = json.loads(str(model.invocations[0][1].content))
        assert initial["execution_tools"] == [] and initial["available_skills"] == []
        returns = [message for message in model.invocations[-1] if isinstance(message, ToolMessage)]
        assert len(returns) == 2 and all(message.status == "error" for message in returns)
        assert "PRIVATE-UNSELECTED-CONTENT" not in str(model.invocations)
        assert not any(record["phase"] == "started" for record in read_records(tmp_path))


def test_planning_rejects_changed_selection_before_calling_model(tmp_path: Path) -> None:
    resource = tmp_path / "skills" / "first.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("Reference", encoding="utf-8")
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as patch:
        model = ScriptedChatModelClient(build_model_profile())
        install_model(client, model, patch)
        selections = capability_selection(client)
        (resource.parent / "added.txt").write_text("New reference", encoding="utf-8")
        case = client.post("/api/test-cases", json={"name": "目录变化", "source_text": "检查标题"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        conflict = client.post(url, json={"planning_request_id": str(uuid4()), "selected_columns": selections})
        assert conflict.status_code == 409 and conflict.json()["code"] == "capability_catalog_changed"
        assert model.invocations == [] and client.get(url).json()["items"] == []
        assert client.post(url, json={"planning_request_id": str(uuid4())}).status_code == 422
        assert client.post(url, json={"planning_request_id": str(uuid4()), "selected_columns": [selections[0]] * 2}).status_code == 422


def test_planning_capacity_failure_keeps_existing_plan_and_does_not_invoke_model(tmp_path: Path) -> None:
    with build_client(tmp_path) as client, pytest.MonkeyPatch.context() as patch:
        model = ScriptedChatModelClient(build_model_profile())
        install_model(client, model, patch)
        case = client.post("/api/test-cases", json={"name": "容量边界", "source_text": "检查标题"}).json()
        url = f"/api/test-cases/{case['id']}/plans"
        first = client.post(url, json={"planning_request_id": str(uuid4()), "selected_columns": []}).json()
        failed = client.post(url, json={
            "planning_request_id": str(uuid4()), "selected_columns": [], "user_input": "必须保留的本轮输入" * 10_000,
        })
        assert failed.status_code == 502 and "input budget" in failed.json()["message"]
        assert len(model.invocations) == 1
        assert client.get(url).json()["items"] == [first]
