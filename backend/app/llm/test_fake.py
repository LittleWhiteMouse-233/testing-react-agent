from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import PrivateAttr

from app.config import LLMProfileSettings
from app.domain.planning import (
    TestCaseContent,
    TestPlanContent,
    TestTaskDefinition,
    TestTaskType,
)
from app.llm.client import profile_snapshot_from_settings


PlanDraft = TestPlanContent[TestTaskDefinition]


def _default_plan(request: TestCaseContent) -> PlanDraft:
    goal = request.source_text
    return TestPlanContent[TestTaskDefinition](
        title=f"执行：{request.name}",
        setup_steps=["确认电视已开机并可被控制"],
        assumptions=[],
        tasks=[
            TestTaskDefinition(
                type=TestTaskType.ACT,
                title="到达目标页面",
                goal=goal,
                success_criteria=["当前截图显示用例描述的目标页面或目标状态"],
                max_cycles=10,
            ),
            TestTaskDefinition(
                type=TestTaskType.JUDGE,
                title="验证预期结果",
                goal=goal,
                success_criteria=["当前截图满足用例中的预期结果"],
                max_cycles=2,
            ),
        ],
    )


class ScriptedChatModel(BaseChatModel):
    _plans: deque[Any] = PrivateAttr()
    _turns: deque[Any] = PrivateAttr()
    _invocations: list[list[BaseMessage]] = PrivateAttr()

    def __init__(
        self,
        *,
        plans: list[PlanDraft | dict[str, Any] | Exception] | None = None,
        turns: list[AIMessage | dict[str, Any] | Exception] | None = None,
    ) -> None:
        super().__init__()
        self._plans = deque(plans or [])
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
        return ChatResult(generations=[ChatGeneration(message=self._next_turn())])

    def _next_turn(self) -> AIMessage:
        value: Any = (
            self._turns.popleft()
            if self._turns
            else {
                "status": "blocked",
                "summary": "No execution turns were configured for the scripted model",
            }
        )
        if isinstance(value, Exception):
            raise value
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
                    "args": {"status": status, "summary": summary},
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
        async def invoke(messages: list[BaseMessage]) -> AIMessage:
            self._invocations.append(list(messages))
            return self._next_turn()

        return RunnableLambda(invoke)

    def with_structured_output(
        self,
        schema: dict[str, Any] | type | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        async def invoke(messages: Any) -> PlanDraft:
            self._invocations.append(list(messages))
            if self._plans:
                value = self._plans.popleft()
                if isinstance(value, Exception):
                    raise value
                return (
                    value
                    if isinstance(value, TestPlanContent)
                    else PlanDraft.model_validate(value)
                )
            return _default_plan(_request_from_messages(messages))

        return RunnableLambda(invoke)


def _request_from_messages(messages: Any) -> TestCaseContent:
    for message in reversed(list(messages)):
        content = getattr(message, "content", "")
        if isinstance(content, str):
            try:
                return TestCaseContent.model_validate(json.loads(content))
            except (ValueError, TypeError):
                continue
    return TestCaseContent(name="脚本测试", source_text="脚本测试")


class ScriptedChatModelClient:
    """可注入计划与回合的确定性 profile 客户端，仅供测试和本地模式。"""

    def __init__(
        self,
        settings: LLMProfileSettings | None = None,
        *,
        model_id: str = "default",
        timeout_seconds: float = 60,
        plans: list[PlanDraft | dict[str, Any] | Exception] | None = None,
        turns: list[AIMessage | dict[str, Any] | Exception] | None = None,
    ) -> None:
        profile = settings or LLMProfileSettings(
            id=model_id,
            mode="scripted",
            model="deterministic",
            timeout_seconds=timeout_seconds,
        )
        self.model_id = profile.id
        self.timeout_seconds = profile.timeout_seconds
        self._model = ScriptedChatModel(plans=plans, turns=turns)
        self.profile_snapshot = profile_snapshot_from_settings(profile)

    def create_model(self) -> BaseChatModel:
        return self._model

    @property
    def invocations(self) -> list[list[BaseMessage]]:
        return self._model.invocations
