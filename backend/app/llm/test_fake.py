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

from app.domain.execution import TaskTerminalDecision
from app.domain.planning import PlanOutput, PlanRequest, Task, TaskType


def _default_plan(request: PlanRequest) -> PlanOutput:
    return PlanOutput(
        title=f"执行：{request.text[:80]}",
        setup_steps=["确认电视已开机并可被控制"],
        assumptions=[],
        tasks=[
            Task(
                task_id="task-1",
                type=TaskType.ACT,
                title="到达目标页面",
                goal=request.text,
                success_criteria=["当前截图显示用例描述的目标页面或目标状态"],
                max_cycles=10,
            ),
            Task(
                task_id="task-2",
                type=TaskType.JUDGE,
                title="验证预期结果",
                goal=request.text,
                success_criteria=["当前截图满足用例中的预期结果"],
                max_cycles=2,
            ),
        ],
    )


class ScriptedChatModel(BaseChatModel):
    _plans: deque[Any] = PrivateAttr()
    _turns: deque[Any] = PrivateAttr()

    def __init__(
        self,
        *,
        plans: list[PlanOutput | dict[str, Any] | Exception] | None = None,
        turns: list[AIMessage | TaskTerminalDecision | dict[str, Any] | Exception]
        | None = None,
    ) -> None:
        super().__init__()
        self._plans = deque(plans or [])
        self._turns = deque(turns or [])

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
            else TaskTerminalDecision(
                status="passed",
                summary="脚本模型确认当前截图满足任务成功标准",
            )
        )
        if isinstance(value, Exception):
            raise value
        if isinstance(value, AIMessage):
            return value
        if isinstance(value, TaskTerminalDecision):
            terminal = value
        elif isinstance(value, dict) and "tool_calls" in value:
            return AIMessage(
                content=value.get("content", ""),
                tool_calls=value["tool_calls"],
            )
        else:
            terminal = TaskTerminalDecision.model_validate(value)
        return AIMessage(content="", additional_kwargs={"parsed": terminal})

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: dict | str | bool | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        async def invoke(_: Any) -> AIMessage:
            return self._next_turn()

        return RunnableLambda(invoke)

    def with_structured_output(
        self,
        schema: dict[str, Any] | type | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        async def invoke(messages: Any) -> Any:
            value: Any
            if self._plans:
                value = self._plans.popleft()
                if isinstance(value, Exception):
                    raise value
                return (
                    value
                    if isinstance(value, PlanOutput)
                    else PlanOutput.model_validate(value)
                )
            request = _request_from_messages(messages)
            return _default_plan(request)

        return RunnableLambda(invoke)


def _request_from_messages(messages: Any) -> PlanRequest:
    for message in reversed(list(messages)):
        content = getattr(message, "content", "")
        if isinstance(content, str):
            try:
                return PlanRequest.model_validate(json.loads(content))
            except (ValueError, TypeError):
                continue
    return PlanRequest(test_case_id="scripted", text="脚本测试")


class ScriptedChatModelProvider:
    def __init__(
        self,
        plans: list[PlanOutput | dict[str, Any] | Exception] | None = None,
        turns: list[AIMessage | TaskTerminalDecision | dict[str, Any] | Exception]
        | None = None,
    ) -> None:
        self._model = ScriptedChatModel(plans=plans, turns=turns)
        self.model_info: dict[str, object] = {
            "provider": "scripted",
            "model": "deterministic",
        }

    def create_model(self) -> BaseChatModel:
        return self._model
