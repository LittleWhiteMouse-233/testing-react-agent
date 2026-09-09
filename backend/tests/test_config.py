from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest
from pydantic import ValidationError

from app.config import LLMProfileSettings, PROJECT_ROOT, Settings


def test_settings_use_device_neutral_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEBUG", "release")
    monkeypatch.setenv("TEST_AGENT_DEBUG", "true")
    monkeypatch.setenv("TEST_AGENT_DATA_DIR", "./config-test-data")
    monkeypatch.setenv(
        "LLM_PROFILES",
        json.dumps(
            [
                {
                    "id": "planner",
                    "model": "planning-model",
                    "context_window_tokens": 16_384,
                    "max_output_tokens": 1_024,
                },
                {
                    "id": "vision",
                    "model": "vision-model",
                    "context_window_tokens": 32_768,
                    "tokens_per_image": 1_500,
                },
            ]
        ),
    )
    monkeypatch.setenv("TEST_AGENT_PLANNING_MODEL_ID", "planner")
    monkeypatch.setenv("TEST_AGENT_EXECUTION_MODEL_ID", "vision")

    settings_factory = cast(Callable[..., Settings], Settings)
    settings = settings_factory(_env_file=None)

    assert settings.debug is True
    assert settings.data_dir == PROJECT_ROOT / "config-test-data"
    assert [profile.id for profile in settings.llm_profiles] == ["planner", "vision"]
    assert settings.planning_model_id == "planner"
    assert settings.execution_model_id == "vision"
    assert settings.llm_profiles[0].context_window_tokens == 16_384
    assert settings.llm_profiles[0].max_output_tokens == 1_024
    assert settings.llm_profiles[1].tokens_per_image == 1_500


def test_llm_profile_rejects_unknown_activity_markers_and_invalid_budget() -> None:
    with pytest.raises(ValidationError, match="activities"):
        LLMProfileSettings.model_validate(
            {"id": "vision", "activities": ["act", "judge"]}
        )
    with pytest.raises(ValidationError, match="smaller than"):
        LLMProfileSettings(
            id="invalid",
            context_window_tokens=2_048,
            max_output_tokens=1_024,
            context_safety_margin_tokens=1_024,
        )
