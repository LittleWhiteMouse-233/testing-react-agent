"""Agent prompt 文本、内容指纹与选择规则的单一资源边界。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.domain.activity import AgentActivity
from app.domain.planning import TestTaskType


@dataclass(frozen=True)
class PromptDefinition:
    """同一次 bytes 读取派生的 prompt 名称、文本与不可变内容指纹。"""

    name: str
    text: str
    fingerprint: str


class PromptCatalog:
    """进程启动时按枚举名称加载的 prompt 集合，统一负责选择和回退。"""

    def __init__(self, *, prompt_directory: Path | None = None) -> None:
        directory = prompt_directory or Path(__file__).resolve().parent
        self._prompts_by_name: dict[str, PromptDefinition | None] = {}
        for prompt_name in (*AgentActivity, *TestTaskType):
            try:
                raw_content = (directory / f"{prompt_name.value}.txt").read_bytes()
            except FileNotFoundError:
                self._prompts_by_name[prompt_name.value] = None
                continue
            self._prompts_by_name[prompt_name.value] = PromptDefinition(
                name=prompt_name.value,
                text=raw_content.decode("utf-8"),
                fingerprint=hashlib.sha256(raw_content).hexdigest()[:12],
            )

    def prompt_for_activity(self, activity: AgentActivity) -> PromptDefinition | None:
        return self._prompts_by_name[activity.value]

    def prompt_for_task_type(self, task_type: TestTaskType) -> PromptDefinition | None:
        prompt = self._prompts_by_name[task_type.value]
        if prompt is not None:
            return prompt
        return self.prompt_for_activity(AgentActivity.EXECUTION)

    @property
    def prompt_fingerprints_by_task_type(self) -> dict[TestTaskType, str | None]:
        prompt_fingerprints: dict[TestTaskType, str | None] = {}
        for task_type in TestTaskType:
            prompt = self.prompt_for_task_type(task_type)
            prompt_fingerprints[task_type] = prompt.fingerprint if prompt is not None else None
        return prompt_fingerprints
