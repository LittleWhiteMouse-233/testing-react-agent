from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import PrivateAttr

from app.domain.resources import LLMProfileSettings
from app.domain.planning import (
    TestCaseContent,
    TestPlanContent,
    TestTaskDefinition,
    TestTaskType,
)


PlanDraft = TestPlanContent[TestTaskDefinition]
ScriptedTurn = AIMessage | dict[str, Any] | Exception | Callable[[list[BaseMessage]], AIMessage]


def _default_plan(request: TestCaseContent) -> PlanDraft:
    goal = request.source_text
    return TestPlanContent[TestTaskDefinition](
        title=f"执行：{request.name}",
        setup_steps=["确认电视已开机并可被控制"],
        assumptions=[],
        tasks=[
            TestTaskDefinition(
                task_no=1,
                type=TestTaskType.ACT,
                title="到达目标页面",
                goal=goal,
                success_criteria=["当前截图显示用例描述的目标页面或目标状态"],
            ),
            TestTaskDefinition(
                task_no=2,
                type=TestTaskType.JUDGE,
                title="验证预期结果",
                goal=goal,
                success_criteria=["当前截图满足用例中的预期结果"],
            ),
        ],
    )


class ScriptedChatModel(BaseChatModel):
    _planning_turns: deque[ScriptedTurn] = PrivateAttr()
    _turns: deque[ScriptedTurn] = PrivateAttr()
    _invocations: list[list[BaseMessage]] = PrivateAttr()

    def __init__(
        self,
        *,
        planning_turns: list[ScriptedTurn] | None = None,
        turns: list[ScriptedTurn] | None = None,
    ) -> None:
        super().__init__()
        self._planning_turns = deque(planning_turns or [])
        self._turns = deque(turns or [])
        self._invocations = []

    @property
    def invocations(self) -> list[list[BaseMessage]]:
        return self._invocations

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_turn(messages))])

    def _next_turn(self, messages: list[BaseMessage], *, planning: bool = False) -> AIMessage:
        scripted_turns = self._planning_turns if planning else self._turns
        if scripted_turns:
            value = scripted_turns.popleft()
        elif planning:
            value = AIMessage(content="", tool_calls=[{
                "name": "finish_plan", "args": _default_plan(_request_from_messages(messages)).model_dump(mode="json"),
                "id": "finish-plan", "type": "tool_call",
            }])
        else:
            value = {
                "status": "blocked",
                "summary": "No execution turns were configured for the scripted model",
            }
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(messages)
        if isinstance(value, AIMessage):
            return value.model_copy(deep=True)
        if isinstance(value, dict) and "tool_calls" in value:
            return AIMessage(
                content=value.get("content", ""), tool_calls=value["tool_calls"]
            )
        if not isinstance(value, dict):
            raise TypeError(f"Unsupported scripted turn: {type(value).__name__}")
        status = str(value.get("status", ""))
        summary = str(value.get("summary", ""))
        if status not in {"passed", "failed", "blocked"} or not summary:
            raise ValueError("Scripted terminal turn is invalid")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "finish_task",
                    "args": {"status": status, "summary": summary, "memory": value.get("memory")},
                    "id": f"finish-{status}",
                    "type": "tool_call",
                }
            ],
        )

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: dict | str | bool | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        planning = any(getattr(bound_tool, "name", None) == "finish_plan" for bound_tool in tools)

        async def invoke(messages: list[BaseMessage]) -> AIMessage:
            self._invocations.append(list(messages))
            return self._next_turn(messages, planning=planning)

        return RunnableLambda(invoke)


def _request_from_messages(messages: list[BaseMessage]) -> TestCaseContent:
    # The fixed planning input owns the case, even when extra input is JSON text.
    content = messages[1].content
    if not isinstance(content, str):
        raise TypeError("Planning test case message must contain JSON text")
    return TestCaseContent.model_validate(json.loads(content)["test_case_content"])


class ScriptedChatModelClient:
    """可注入计划与回合的确定性 profile 客户端，仅供测试和本地模式。"""

    def __init__(
        self,
        profile: LLMProfileSettings,
        *,
        planning_turns: list[ScriptedTurn] | None = None,
        turns: list[ScriptedTurn] | None = None,
    ) -> None:
        self._model = ScriptedChatModel(planning_turns=planning_turns, turns=turns)
        self.profile_snapshot = profile

    @property
    def model_id(self) -> str:
        return self.profile_snapshot.id

    @property
    def timeout_seconds(self) -> float:
        return self.profile_snapshot.timeout_seconds

    def create_model(self) -> BaseChatModel:
        return self._model

    @property
    def invocations(self) -> list[list[BaseMessage]]:
        return self._model.invocations
