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
from app.execution.task_agent import (
    _count_model_request_tokens,
    _select_model_message_window,
    _collect_expired_screenshot_updates,
)
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


def screenshot_message(message_id: str, *, encoded_size: int) -> HumanMessage:
    return HumanMessage(
        id=message_id,
        content=[
            {"type": "text", "text": "1920x1080 当前屏幕截图"},
            {
                "type": "image",
                "base64": "A" * encoded_size,
                "mime_type": "image/png",
            },
        ],
    )


def test_chinese_text_uses_profile_calibrated_conservative_ratio() -> None:
    profile = model_profile(characters_per_token=1.5)
    chinese_text = "这是用于校准上下文窗口的中文测试内容。" * 100
    tokens = _count_model_request_tokens(
        [HumanMessage(content=chinese_text)],
        tools=(),
        model_profile=profile,
    )

    assert tokens >= len(chinese_text) / profile.characters_per_token
    assert tokens > len(chinese_text) / 4


def test_1080p_screenshot_uses_configured_image_budget_not_base64_length() -> None:
    profile = model_profile(tokens_per_image=1_500)
    small_encoding = _count_model_request_tokens(
        [screenshot_message("small", encoded_size=20)],
        tools=(),
        model_profile=profile,
    )
    large_encoding = _count_model_request_tokens(
        [screenshot_message("large", encoded_size=2_000_000)],
        tools=(),
        model_profile=profile,
    )

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

    short_count = _count_model_request_tokens(
        messages,
        tools=(short_tool,),
        model_profile=profile,
    )
    long_count = _count_model_request_tokens(
        messages,
        tools=(long_tool,),
        model_profile=profile,
    )

    assert long_count > short_count + 4_000


def test_window_trims_old_history_and_images_but_keeps_latest_screenshot() -> None:
    profile = model_profile(
        context_window_tokens=4_096,
        max_output_tokens=512,
        context_safety_margin_tokens=512,
        tokens_per_image=800,
    )
    messages = [
        SystemMessage(id="system", content="系统规则"),
        HumanMessage(id="task", content="当前任务及成功标准"),
        HumanMessage(id="old-text", content="旧的中文历史。" * 500),
        screenshot_message("old-image", encoded_size=100_000),
        HumanMessage(id="recent-text", content="最近的工具结果"),
        screenshot_message("latest-image", encoded_size=2_000_000),
    ]
    tools = (structured_tool(),)

    window = _select_model_message_window(
        messages,
        tools=tools,
        model_profile=profile,
    )

    window_ids = {message.id for message in window}
    assert "latest-image" in window_ids
    assert "task" in window_ids
    # Images that fit the token budget remain; decision-round pruning is separate.
    assert "old-image" in window_ids
    assert "old-text" not in window_ids
    assert _count_model_request_tokens(
        window,
        tools=tools,
        model_profile=profile,
    ) <= (
        profile.context_window_tokens
        - profile.max_output_tokens
        - profile.context_safety_margin_tokens
    )


def test_fixed_tool_schema_and_latest_screenshot_over_budget_block_explicitly() -> None:
    profile = model_profile(
        context_window_tokens=2_048,
        max_output_tokens=256,
        context_safety_margin_tokens=256,
        tokens_per_image=1_200,
    )
    long_tool = structured_tool(
        description_length=3_000,
        parameter_description_length=3_000,
    )

    with pytest.raises(ModelContextBudgetExceeded, match="latest screenshot"):
        _select_model_message_window(
            [
                SystemMessage(id="system", content="系统规则"),
                screenshot_message("latest-image", encoded_size=2_000_000),
            ],
            tools=(long_tool,),
            model_profile=profile,
        )


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

    with pytest.raises(ModelContextBudgetExceeded, match="system prompt"):
        _select_model_message_window(
            [oversized_system_prompt, HumanMessage(id="current", content="当前观察")],
            tools=(structured_tool(),),
            model_profile=profile,
        )


@pytest.mark.parametrize("history_rounds", [1, 3])
def test_visual_retention_replaces_expired_messages_without_mutating_history(history_rounds: int) -> None:
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
    replacements = _collect_expired_screenshot_updates(messages, history_rounds)
    projected = cast(list[AnyMessage], add_messages(cast(Messages, messages), cast(Messages, replacements)))
    retained_rounds = (1, 3, 5, 6)[-history_rounds:]
    assert [block.get('id') for message in projected for block in message.content_blocks if block['type'] == 'image'] == [
        artifact_ids[index, n] for index in retained_rounds for n in range(2)
    ]
    assert len(replacements) == 4 - history_rounds
    assert [message.id for message in projected] == [message.id for message in messages]
    assert [project_run_message(message) for message in projected] == [project_run_message(message) for message in messages]
    assert _collect_expired_screenshot_updates(projected, history_rounds) == []
    assert 'outside the visual history' in projected[3].text
    assert [block.get('extras', {}).get('artifact_id') for block in projected[3].content_blocks if 'artifact_id' in block.get('extras', {})] == [artifact_ids[1, n] for n in range(2)]
    assert 'base64' not in projected[3].model_dump_json()
    assert projected[-1] is messages[-1]  # unchanged messages need no deep copy
    assert isinstance(replacements[0].content, list)
    copied_block = replacements[0].content[0]
    assert isinstance(copied_block, dict)
    copied_block['extras']['nested'].append('changed')
    assert [message.model_dump() for message in messages] == originals


@pytest.mark.parametrize("history_rounds", [1, 3])
def test_visual_retention_advances_only_with_new_screenshot_rounds(history_rounds: int) -> None:
    messages = cast(list[AnyMessage], add_messages([], [SystemMessage(content="rules"), HumanMessage(content="task")]))
    screenshot_rounds: list[int] = []
    for decision_round in range(1, 13):
        content: list[str | dict[str, Any]] = [{"type": "text", "text": f"round {decision_round}"}]
        if decision_round in (1, 3, 5, 6, 9):
            screenshot_rounds.append(decision_round)
            content.extend({"type": "image", "base64": "AA==", "mime_type": "image/png",
                            "id": f"{decision_round}-{index}"} for index in range(2))
        messages = cast(list[AnyMessage], add_messages(cast(Messages, messages), [
            AIMessage(content="", tool_calls=[{"name": "inspect", "args": {}, "id": str(decision_round)}]),
            ToolMessage(content=content, tool_call_id=str(decision_round)),
        ]))
        replacements = _collect_expired_screenshot_updates(messages, history_rounds)
        messages = cast(list[AnyMessage], add_messages(cast(Messages, messages), cast(Messages, replacements)))
        assert [block.get("id") for message in messages for block in message.content_blocks if block["type"] == "image"] == [
            f"{round_number}-{index}" for round_number in screenshot_rounds[-history_rounds:] for index in range(2)
        ]
        assert len(messages) == 2 + 2 * decision_round
        assert _collect_expired_screenshot_updates(messages, history_rounds) == []
