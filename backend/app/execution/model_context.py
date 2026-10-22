"""Task-local model input budgeting and required request context selection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool

from app.domain.errors import ModelContextBudgetExceeded
from app.domain.resources.llm import LLMProfileSnapshot
from app.skill import LOAD_SKILL_TOOL_NAME


class ModelContextWindow:
    """Bind a task's frozen model profile and complete tools without retaining messages."""

    def __init__(
        self, *, model_profile: LLMProfileSnapshot, tools: tuple[BaseTool, ...],
    ) -> None:
        self._model_profile = model_profile
        self._tools = tools

    @property
    def _input_budget_tokens(self) -> int:
        return (
            self._model_profile.context_window_tokens
            - self._model_profile.max_output_tokens
            - self._model_profile.context_safety_margin_tokens
        )

    def estimate_request_tokens(self, messages: Sequence[BaseMessage]) -> int:
        """Estimate message envelopes, images and the complete tool schemas."""

        return count_tokens_approximately(
            messages,
            chars_per_token=self._model_profile.characters_per_token,
            tokens_per_image=self._model_profile.tokens_per_image,
            tools=list(self._tools),
        )

    def exceeds_input_budget(self, token_count: int) -> bool:
        return token_count > self._input_budget_tokens

    def select_messages(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        """Select a request without changing State; reject loss of required context.

        LangChain owns token estimation and conversational suffix trimming. This
        product boundary retains the task, loaded Skills and latest observation.
        """

        request_messages = list(messages)
        prefix_end = next(
            (index + 1 for index, message in enumerate(request_messages) if isinstance(message, HumanMessage)),
            0,
        )
        # Keep each Skill's latest successful instructions and their original
        # call/return objects, including images already replaced by State retention.
        skill_calls: dict[str, tuple[int, tuple[str, str]]] = {}
        loaded_skill_indices: dict[tuple[str, str], tuple[int, int]] = {}
        for index, message in enumerate(request_messages):
            if isinstance(message, AIMessage) and len(message.tool_calls) == 1:
                call = message.tool_calls[0]
                if call["name"] == LOAD_SKILL_TOOL_NAME and (call_id := call.get("id")):
                    skill_calls[call_id] = (index, (call["args"].get("source_id", ""), call["args"].get("uri", "")))
            elif isinstance(message, ToolMessage) and message.status != "error" and message.tool_call_id in skill_calls:
                call_index, skill_address = skill_calls[message.tool_call_id]
                loaded_skill_indices[skill_address] = (call_index, index)
        required_indices = set(range(prefix_end)) | {index for pair in loaded_skill_indices.values() for index in pair}
        required_messages = [message for index, message in enumerate(request_messages) if index in required_indices]
        suffix = trim_messages(
            [message for index, message in enumerate(request_messages) if index not in required_indices],
            max_tokens=self._input_budget_tokens,
            token_counter=lambda candidates: self.estimate_request_tokens([*required_messages, *cast(list[BaseMessage], candidates)]),
            strategy="last",
            allow_partial=False,
            start_on=("human", "ai"),
            end_on=("human", "tool"),
        )
        suffix_objects = {id(message) for message in suffix}
        window = [message for index, message in enumerate(request_messages) if index in required_indices or id(message) in suffix_objects]
        if self.exceeds_input_budget(self.estimate_request_tokens(window)):
            raise ModelContextBudgetExceeded(
                "The required system prompt, task context, loaded Skills and fixed tool schemas exceed the model input budget"
            )
        if messages and messages[-1] not in window:
            raise ModelContextBudgetExceeded("The latest tool result or task context exceeds the model input budget")
        # A retained tool return must remain paired with its original call.
        retained_calls = {
            call["id"] for message in window if isinstance(message, AIMessage)
            for call in message.tool_calls
        }
        retained_returns = {message.tool_call_id for message in window if isinstance(message, ToolMessage)}
        if retained_calls != retained_returns:
            raise ModelContextBudgetExceeded("The tool call and return pair cannot fit the model input budget")
        return window
