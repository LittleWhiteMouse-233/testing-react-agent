from __future__ import annotations

import pytest
from typing import Any, cast
from uuid import uuid4
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph.message import Messages, add_messages
from pydantic import Field, create_model

from app.domain.errors import ModelContextBudgetExceeded
from app.domain.resources.llm import LLMProfileSnapshot
from app.execution.model_context import ModelContextWindow, ModelImageWindow
from app.execution.task_agent import finish_task
from app.event_stream.projector import project_run_message


def model_profile(
    *,
    context_window_tokens: int = 8_192,
    max_output_tokens: int = 1_024,
    characters_per_token: float = 1.5,
    tokens_per_image: int = 1_024,
    context_safety_margin_tokens: int = 512,
) -> LLMProfileSnapshot:
    return LLMProfileSnapshot(
        profile_id="window-test",
        provider="scripted",
        model="deterministic",
        base_url=None,
        temperature=0,
        timeout_seconds=60,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        characters_per_token=characters_per_token,
        tokens_per_image=tokens_per_image,
        context_safety_margin_tokens=context_safety_margin_tokens,
    )


def structured_tool(
    *,
    name: str = "inspect_screen",
    description_length: int = 20,
    parameter_description_length: int = 20,
) -> StructuredTool:
    args_schema = create_model(
        f"{name.title().replace('_', '')}Args",
        query=(
            str,
            Field(
                min_length=1,
                description="p" * parameter_description_length,
            ),
        ),
    )

    def invoke(query: str) -> str:
        return query

    return StructuredTool.from_function(
        invoke,
        name=name,
        description="d" * description_length,
        args_schema=args_schema,
    )


def image_message(message_id: str, *, encoded_size: int) -> HumanMessage:
    return HumanMessage(
        id=message_id,
        content=[
            {"type": "text", "text": "1920x1080 当前屏幕截图"},
            {
                "type": "image",
                "id": f"artifact-{message_id}",
                "base64": "A" * encoded_size,
                "mime_type": "image/png",
            },
        ],
    )


def observation_messages(
    call_id: str, observation_content: str | list[str | dict[str, Any]], *, query: str = "current state",
) -> tuple[AIMessage, ToolMessage]:
    return (
        AIMessage(id=f"ai-{call_id}", content="", tool_calls=[{
            "id": call_id, "name": "inspect_screen", "args": {"query": query},
        }]),
        ToolMessage(id=f"tool-{call_id}", name="inspect_screen", tool_call_id=call_id, content=observation_content),
    )


@pytest.mark.parametrize("token_count,exceeded", [(3_071, False), (3_072, False), (3_073, True)])
def test_input_budget_boundary(token_count: int, exceeded: bool) -> None:
    context_window = ModelContextWindow(
        model_profile=model_profile(
            context_window_tokens=4_096, max_output_tokens=512, context_safety_margin_tokens=512,
        ),
        tools=(),
    )

    assert context_window.exceeds_input_budget(token_count) is exceeded


def test_selection_preserves_history_and_does_not_retain_previous_request() -> None:
    context_window = ModelContextWindow(
        model_profile=model_profile(context_window_tokens=3_000), tools=(structured_tool(), finish_task),
    )
    messages = [
        SystemMessage(id="system", content="rules"),
        HumanMessage(id="task", content="task"),
        *observation_messages("old", "Old observations" * 1_000),
        *observation_messages("latest", "Latest observation"),
    ]
    originals = [message.model_dump() for message in messages]

    window = context_window.select_messages(messages)

    assert [message.id for message in window] == ["system", "task", "ai-latest", "tool-latest"]
    assert [message.model_dump() for message in messages] == originals
    next_messages = [*messages[:2], *observation_messages("next", "Next observation")]
    assert [message.id for message in context_window.select_messages(next_messages)] == [
        "system", "task", "ai-next", "tool-next",
    ]


def test_chinese_text_uses_profile_calibrated_conservative_ratio() -> None:
    profile = model_profile(characters_per_token=1.5)
    context_window = ModelContextWindow(model_profile=profile, tools=(finish_task,))
    chinese_text = "这是用于校准上下文窗口的中文测试内容。" * 100
    tokens = context_window.estimate_request_tokens([HumanMessage(content=chinese_text)])

    assert tokens >= len(chinese_text) / profile.characters_per_token
    assert tokens > len(chinese_text) / 4


def test_1080p_image_uses_configured_image_budget_not_base64_length() -> None:
    profile = model_profile(tokens_per_image=1_500)
    context_window = ModelContextWindow(model_profile=profile, tools=(finish_task,))
    small_encoding = context_window.estimate_request_tokens([image_message("small", encoded_size=20)])
    large_encoding = context_window.estimate_request_tokens([image_message("large", encoded_size=2_000_000)])

    assert small_encoding == large_encoding
    assert small_encoding >= profile.tokens_per_image


def test_long_tool_description_and_parameter_schema_are_counted() -> None:
    profile = model_profile()
    messages = [HumanMessage(content="检查当前屏幕")]
    short_tool = structured_tool()
    long_tool = structured_tool(
        name="inspect_screen_deeply",
        description_length=4_000,
        parameter_description_length=4_000,
    )

    short_count = ModelContextWindow(model_profile=profile, tools=(short_tool, finish_task)).estimate_request_tokens(messages)
    long_count = ModelContextWindow(model_profile=profile, tools=(long_tool, finish_task)).estimate_request_tokens(messages)

    assert long_count > short_count + 4_000


def test_window_trims_previous_image_before_latest_text_observation() -> None:
    context_window = ModelContextWindow(
        model_profile=model_profile(context_window_tokens=3_000), tools=(structured_tool(), finish_task),
    )
    prefix = [SystemMessage(id="system", content="rules"), HumanMessage(id="task", content="task")]
    latest_observation = observation_messages("latest", "Current logs support the goal" * 30)
    messages = [
        *prefix,
        *observation_messages("image", image_message("history-image", encoded_size=20).content),
        *latest_observation,
    ]
    originals = [message.model_dump() for message in messages]
    assert context_window.exceeds_input_budget(context_window.estimate_request_tokens(messages))
    assert not context_window.exceeds_input_budget(context_window.estimate_request_tokens([*prefix, *latest_observation]))

    window = context_window.select_messages(messages)

    assert [message.id for message in window] == ["system", "task", "ai-latest", "tool-latest"]
    assert [message.model_dump() for message in messages] == originals


def test_window_preserves_images_that_fit_after_history_trimming() -> None:
    profile = model_profile(
        context_window_tokens=4_096,
        max_output_tokens=512,
        context_safety_margin_tokens=512,
        tokens_per_image=800,
    )
    messages = [
        SystemMessage(id="system", content="系统规则"),
        HumanMessage(id="task", content="当前任务及成功标准"),
        *observation_messages("old-text", "旧的中文历史。" * 500),
        *observation_messages("old-image", image_message("old-image", encoded_size=100_000).content),
        *observation_messages("recent-text", "最近的工具结果"),
        *observation_messages("latest-image", image_message("latest-image", encoded_size=2_000_000).content),
    ]
    context_window = ModelContextWindow(model_profile=profile, tools=(structured_tool(), finish_task))
    assert context_window.exceeds_input_budget(context_window.estimate_request_tokens(messages))
    assert not context_window.exceeds_input_budget(context_window.estimate_request_tokens([*messages[:2], *messages[4:]]))

    window = context_window.select_messages(messages)

    assert [message.id for message in window] == [
        "system", "task", "ai-old-image", "tool-old-image", "ai-recent-text", "tool-recent-text",
        "ai-latest-image", "tool-latest-image",
    ]
    assert not context_window.exceeds_input_budget(context_window.estimate_request_tokens(window))


def test_fixed_tool_schema_over_budget_blocks_explicitly() -> None:
    profile = model_profile(
        context_window_tokens=2_048,
        max_output_tokens=256,
        context_safety_margin_tokens=256,
    )
    long_tool = structured_tool(
        description_length=3_000,
        parameter_description_length=3_000,
    )
    context_window = ModelContextWindow(model_profile=profile, tools=(long_tool, finish_task))
    assert context_window.exceeds_input_budget(context_window.estimate_request_tokens([]))

    with pytest.raises(ModelContextBudgetExceeded, match="fixed tool schemas"):
        context_window.select_messages(
            [
                SystemMessage(id="system", content="系统规则"),
                HumanMessage(id="task", content="当前任务及成功标准"),
            ],
        )


@pytest.mark.parametrize("observation_kind", ["text", "image"])
def test_latest_tool_result_over_budget_blocks(observation_kind: str) -> None:
    context_window = ModelContextWindow(
        model_profile=model_profile(
            context_window_tokens=2_048, max_output_tokens=256,
            context_safety_margin_tokens=256, tokens_per_image=2_000,
        ),
        tools=(structured_tool(), finish_task),
    )
    prefix = [SystemMessage(content="rules"), HumanMessage(content="task")]
    observation_content = (
        image_message("latest-image", encoded_size=20).content
        if observation_kind == "image" else "Latest required observation" * 500
    )
    latest_call, latest_result = observation_messages("latest", observation_content)
    assert not context_window.exceeds_input_budget(context_window.estimate_request_tokens([*prefix, latest_call]))
    assert context_window.exceeds_input_budget(context_window.estimate_request_tokens([*prefix, latest_result]))

    with pytest.raises(ModelContextBudgetExceeded):
        context_window.select_messages([*prefix, latest_call, latest_result])


def test_latest_tool_call_and_result_must_fit_together() -> None:
    profile = model_profile()
    tools = (structured_tool(), finish_task)
    context_window = ModelContextWindow(model_profile=profile, tools=tools)
    prefix = [SystemMessage(content="rules"), HumanMessage(content="task")]
    latest_call, latest_result = observation_messages(
        "latest", "Observed current state. " * 20, query="Describe the current screen. " * 20,
    )
    individual_budget = max(
        context_window.estimate_request_tokens([*prefix, latest_call]),
        context_window.estimate_request_tokens([*prefix, latest_result]),
    )
    context_window = ModelContextWindow(
        model_profile=profile.model_copy(update={
            "context_window_tokens": individual_budget + profile.max_output_tokens + profile.context_safety_margin_tokens,
        }),
        tools=tools,
    )
    assert not context_window.exceeds_input_budget(context_window.estimate_request_tokens([*prefix, latest_call]))
    assert not context_window.exceeds_input_budget(context_window.estimate_request_tokens([*prefix, latest_result]))
    assert context_window.exceeds_input_budget(context_window.estimate_request_tokens([*prefix, latest_call, latest_result]))

    with pytest.raises(ModelContextBudgetExceeded):
        context_window.select_messages([*prefix, latest_call, latest_result])


def test_system_prompt_is_never_silently_trimmed() -> None:
    profile = model_profile(
        context_window_tokens=2_048,
        max_output_tokens=256,
        context_safety_margin_tokens=256,
    )
    oversized_system_prompt = SystemMessage(
        id="system",
        content="系统判定规则" * 500,
    )
    context_window = ModelContextWindow(model_profile=profile, tools=(structured_tool(), finish_task))

    with pytest.raises(ModelContextBudgetExceeded, match="system prompt"):
        context_window.select_messages(
            [oversized_system_prompt, HumanMessage(id="current", content="当前观察")],
        )


def skill_messages(call_id: str, body: str, *, uri: str = "skill://parent/SKILL.md", status: str = "success") -> list[AnyMessage]:
    return [AIMessage(id=f"ai-{call_id}", content="", tool_calls=[{
        "id": call_id, "name": "load_skill", "args": {"source_id": "mcp:fixture", "uri": uri},
    }]), ToolMessage(id=f"tool-{call_id}", name="load_skill", tool_call_id=call_id, content=body, status=status)]  # type: ignore[arg-type]


def test_loaded_skills_survive_trimming_with_latest_successful_version() -> None:
    messages: list[AnyMessage] = [SystemMessage(id="system", content="rules"), HumanMessage(id="task", content="task")]
    messages.extend(skill_messages("old", "superseded body" * 800))
    messages.extend(skill_messages("current", "Current Skill instructions"))
    messages.extend(skill_messages("child", "Child Skill instructions", uri="skill://parent/child/SKILL.md"))
    messages.extend(skill_messages("failed", "Cannot load", status="error"))
    messages.extend([
        AIMessage(id="history-ai", content="", tool_calls=[{"id": "history", "name": "observe", "args": {}}]),
        ToolMessage(id="history-tool", content="Old observations" * 1_500, tool_call_id="history"),
        AIMessage(id="latest-ai", content="", tool_calls=[{"id": "latest", "name": "observe", "args": {}}]),
        ToolMessage(id="latest-tool", content="Latest observation", tool_call_id="latest"),
    ])
    context_window = ModelContextWindow(model_profile=model_profile(context_window_tokens=3_000), tools=(finish_task,))
    window = context_window.select_messages(messages)
    ids = {entry.id for entry in window}
    assert {"ai-current", "tool-current", "ai-child", "tool-child", "latest-ai", "latest-tool"} <= ids
    assert "tool-old" not in ids and "history-tool" not in ids
    assert [entry.id for entry in window] == [entry.id for entry in messages if entry.id in ids]


def test_loaded_skill_over_budget_blocks_instead_of_losing_instructions() -> None:
    messages: list[AnyMessage] = [SystemMessage(content="rules"), HumanMessage(content="task")]
    messages.extend(skill_messages("large", "Required instructions" * 1_000))
    messages.extend([AIMessage(content="", tool_calls=[{"id": "latest", "name": "observe", "args": {}}]),
                     ToolMessage(content="Latest observation", tool_call_id="latest")])
    context_window = ModelContextWindow(model_profile=model_profile(context_window_tokens=3_000), tools=(finish_task,))
    with pytest.raises(ModelContextBudgetExceeded):
        context_window.select_messages(messages)


@pytest.mark.parametrize("history_rounds", [1, 3])
def test_image_retention_replaces_expired_messages_without_mutating_history(history_rounds: int) -> None:
    image_window = ModelImageWindow(history_rounds=history_rounds)
    messages: list[AnyMessage] = [SystemMessage(content='rules'), HumanMessage(content='task')]
    artifact_ids = {(decision_round, index): str(uuid4()) for decision_round in (1, 3, 5, 6) for index in range(2)}
    for index in range(1, 10):
        messages.append(AIMessage(content='', tool_calls=[{'name': 'inspect', 'args': {}, 'id': str(index)}]))
        content: list[str | dict[str, Any]] = [{'type': 'text', 'text': str(index), 'extras': {'nested': ['original']}}]
        if index in (1, 3, 5, 6):
            content.extend([{'type': 'image', 'base64': 'AA==', 'mime_type': 'image/png', 'id': artifact_ids[index, n]} for n in range(2)])
        messages.append(ToolMessage(content=content, tool_call_id=str(index)))
    messages = cast(list[AnyMessage], add_messages([], cast(Messages, messages)))
    originals = [message.model_dump() for message in messages]
    replacements = image_window.collect_expired_updates(messages)
    projected = cast(list[AnyMessage], add_messages(cast(Messages, messages), cast(Messages, replacements)))
    retained_rounds = (1, 3, 5, 6)[-history_rounds:]
    assert [block.get('id') for message in projected for block in message.content_blocks if block['type'] == 'image'] == [
        artifact_ids[index, n] for index in retained_rounds for n in range(2)
    ]
    assert len(replacements) == 4 - history_rounds
    assert [message.id for message in projected] == [message.id for message in messages]
    assert [project_run_message(message) for message in projected] == [project_run_message(message) for message in messages]
    assert image_window.collect_expired_updates(projected) == []
    assert 'outside the image history' in projected[3].text
    assert [block.get('extras', {}).get('artifact_id') for block in projected[3].content_blocks if 'artifact_id' in block.get('extras', {})] == [artifact_ids[1, n] for n in range(2)]
    assert 'base64' not in projected[3].model_dump_json()
    assert projected[-1] is messages[-1]  # unchanged messages need no deep copy
    assert isinstance(replacements[0].content, list)
    copied_block = replacements[0].content[0]
    assert isinstance(copied_block, dict)
    copied_block['extras']['nested'].append('changed')
    assert [message.model_dump() for message in messages] == originals


@pytest.mark.parametrize("history_rounds", [1, 3])
def test_image_retention_advances_only_with_new_image_rounds(history_rounds: int) -> None:
    image_window = ModelImageWindow(history_rounds=history_rounds)
    messages = cast(list[AnyMessage], add_messages([], [SystemMessage(content="rules"), HumanMessage(content="task")]))
    image_rounds: list[int] = []
    for decision_round in range(1, 13):
        content: list[str | dict[str, Any]] = [{"type": "text", "text": f"round {decision_round}"}]
        if decision_round in (1, 3, 5, 6, 9):
            image_rounds.append(decision_round)
            content.extend({"type": "image", "base64": "AA==", "mime_type": "image/png",
                            "id": f"{decision_round}-{index}"} for index in range(2))
        messages = cast(list[AnyMessage], add_messages(cast(Messages, messages), [
            AIMessage(content="", tool_calls=[{"name": "inspect", "args": {}, "id": str(decision_round)}]),
            ToolMessage(content=content, tool_call_id=str(decision_round)),
        ]))
        replacements = image_window.collect_expired_updates(messages)
        messages = cast(list[AnyMessage], add_messages(cast(Messages, messages), cast(Messages, replacements)))
        assert [block.get("id") for message in messages for block in message.content_blocks if block["type"] == "image"] == [
            f"{round_number}-{index}" for round_number in image_rounds[-history_rounds:] for index in range(2)
        ]
        assert len(messages) == 2 + 2 * decision_round
        assert image_window.collect_expired_updates(messages) == []


def test_image_window_does_not_retain_previous_history() -> None:
    image_window = ModelImageWindow(history_rounds=1)
    for history_name in ("first", "second"):
        messages = [
            SystemMessage(id="system", content="rules"),
            HumanMessage(id="task", content=history_name),
            *observation_messages("old", image_message(f"{history_name}-old", encoded_size=20).content),
            *observation_messages("latest", image_message(f"{history_name}-latest", encoded_size=20).content),
        ]

        replacements = image_window.collect_expired_updates(messages)

        assert [message.id for message in replacements] == ["tool-old"]
        assert replacements[0].content_blocks[-1].get("extras") == {
            "artifact_id": f"artifact-{history_name}-old",
        }
        projected = cast(list[AnyMessage], add_messages(cast(Messages, messages), cast(Messages, replacements)))
        assert [block.get("id") for message in projected for block in message.content_blocks if block["type"] == "image"] == [
            f"artifact-{history_name}-latest",
        ]


@pytest.mark.parametrize("history_rounds", [0, -1])
def test_image_window_requires_at_least_one_history_round(history_rounds: int) -> None:
    with pytest.raises(ValueError, match="Image history must retain at least one round"):
        ModelImageWindow(history_rounds=history_rounds)
