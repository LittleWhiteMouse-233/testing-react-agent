"""Complete independent configuration fixtures for host and process tests."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

import pytest

from app import llm
from app.config import PROJECT_ROOT, Settings
from app.domain.activity import AgentActivity
from app.domain.resources import LLMProfileSettings
from app.llm import ChatModelClient
from app.domain.execution import ExecutionSettings

if TYPE_CHECKING:
    from app.container import Container


def model_profile_values(**profile_parameters: object) -> dict[str, Any]:
    return {
        "id": "default", "mode": "scripted", "model": "deterministic",
        "temperature": 0, "timeout_seconds": 60,
        "context_window_tokens": 32768, "max_output_tokens": 2048,
        "characters_per_token": 1.5, "tokens_per_image": 1024,
        "context_safety_margin_tokens": 1024,
        **profile_parameters,
    }


def build_model_profile(**profile_parameters: object) -> LLMProfileSettings:
    return LLMProfileSettings.model_validate(model_profile_values(**profile_parameters))


def app_config_values(
    root: Path, *, execution: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None, **app_parameters: object,
) -> dict[str, Any]:
    return {
        "app_name": "Android TV Test Agent", "app_version": "0.1.0",
        "debug": False, "cors_origins": ["http://localhost:5173"],
        "data_dir": root.as_posix(), "mcp_config_path": (root / "mcp.json").as_posix(),
        "skills_dir": (root / "skills").as_posix(),
        "execution": {
            "tool_call_timeout_seconds": 120, "goal_max_failures": 10,
            "image_history_rounds": 3, "model_call_max_attempts": 3,
            "model_response_max_attempts": 3,
            "max_cycles_by_task_type": {"act": 30, "judge": 10, "goal": 10},
            **(execution or {}),
        },
        "llm": {"profiles": [model_profile_values()], "planning_model_id": "default",
                "execution_model_id": "default", **(llm or {})},
        **app_parameters,
    }


def build_settings(
    root: Path, *, execution: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None, **app_parameters: object,
) -> Settings:
    settings_values = app_config_values(root, execution=execution, llm=llm, **app_parameters)
    settings_values["config_path"] = (PROJECT_ROOT / root / "config.toml").resolve()
    return Settings.model_validate(settings_values)


def install_model_client(monkeypatch: pytest.MonkeyPatch, model_client: ChatModelClient) -> None:
    """在受控测试生命周期内回放指定配置，保留其它配置的生产构造行为。"""
    create_client = llm.create_model_client

    def client_for_profile(profile: LLMProfileSettings) -> ChatModelClient:
        return model_client if profile == model_client.profile_snapshot else create_client(profile)

    monkeypatch.setattr(llm, "create_model_client", client_for_profile)


def build_execution_settings(**parameters: object) -> ExecutionSettings:
    return ExecutionSettings.model_validate({**app_config_values(Path("unused"))["execution"], **parameters})


def execution_model_client(container: Container) -> ChatModelClient:
    """测试从当前配置路由读取客户端以检查实际调用记录。"""
    profile = container.settings_service.current.llm.profile_for_activity(AgentActivity.EXECUTION)
    return llm.create_model_client(profile)


def write_app_config(root: Path, config_values: dict[str, Any] | None = None) -> Path:
    # Framework input payloads also include deliberately invalid validation cases.
    sections = deepcopy(config_values if config_values is not None else app_config_values(root))
    execution = sections.pop("execution", {})
    cycle_limits = execution.pop("max_cycles_by_task_type", {})
    llm = sections.pop("llm", {})
    profiles = llm.pop("profiles", [])

    def assignments(parameters: dict[str, Any]) -> str:
        return "\n".join(
            f"{name} = {json.dumps(value.as_posix() if isinstance(value, Path) else value, ensure_ascii=False)}"
            for name, value in parameters.items()
        )

    paragraphs = [
        assignments(sections), "[execution]", assignments(execution),
        "[execution.max_cycles_by_task_type]", assignments(cycle_limits),
        "[llm]", assignments(llm),
    ]
    if not profiles:
        paragraphs.append("profiles = []")
    for profile in profiles:
        paragraphs.extend(("[[llm.profiles]]", assignments(profile)))
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.toml"
    config_path.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return config_path
