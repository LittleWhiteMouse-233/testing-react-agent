"""Agent prompt 文本与内容版本的单一资源边界。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptDefinition:
    """同一次 bytes 读取派生的 prompt 名称、文本与不可变内容指纹。"""

    name: str
    text: str
    version: str


@dataclass(frozen=True)
class PromptCatalog:
    """进程启动时一次性加载的 planner/act/judge prompt 集合。"""

    planner: PromptDefinition
    act: PromptDefinition
    judge: PromptDefinition


def load_prompt(name: str, *, prompt_directory: Path | None = None) -> PromptDefinition:
    """读取一次资源 bytes，并从该同一内容解码文本和计算版本。"""

    if name not in {"planner", "act", "judge"}:
        raise ValueError(f"Unknown prompt: {name}")
    directory = prompt_directory or Path(__file__).resolve().parent
    raw_content = (directory / f"{name}.txt").read_bytes()
    return PromptDefinition(
        name=name,
        text=raw_content.decode("utf-8"),
        version=hashlib.sha256(raw_content).hexdigest()[:12],
    )


def load_prompt_catalog(
    *, prompt_directory: Path | None = None
) -> PromptCatalog:
    """在组合根一次加载全部固定 prompt，供记录者和模型调用者共享。"""

    return PromptCatalog(
        planner=load_prompt("planner", prompt_directory=prompt_directory),
        act=load_prompt("act", prompt_directory=prompt_directory),
        judge=load_prompt("judge", prompt_directory=prompt_directory),
    )
