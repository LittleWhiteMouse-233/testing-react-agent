"""Complete independent configuration fixtures for host and process tests."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from app.config import LLMProfileSettings, Settings


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
        "llm": {"profiles": [model_profile_values()], **(llm or {})},
        **app_parameters,
    }


def build_settings(
    root: Path, *, execution: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None, **app_parameters: object,
) -> Settings:
    return Settings.model_validate(app_config_values(
        root, execution=execution, llm=llm, **app_parameters,
    ))


def write_app_config(root: Path, config_values: dict[str, Any] | None = None) -> Path:
    # Framework input payloads also include deliberately invalid validation cases.
    sections = deepcopy(config_values if config_values is not None else app_config_values(root))
    execution = sections.pop("execution", {})
    cycle_limits = execution.pop("max_cycles_by_task_type", {})
    llm = sections.pop("llm", {})
    profiles = llm.pop("profiles", [])

    def assignments(parameters: dict[str, Any]) -> str:
        return "\n".join(
            f"{name} = {json.dumps(value, ensure_ascii=False)}"
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
