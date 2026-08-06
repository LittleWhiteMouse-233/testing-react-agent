from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest

from app.config import PROJECT_ROOT, Settings


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
                {"id": "planner", "model": "planning-model"},
                {"id": "vision", "model": "vision-model"},
            ]
        ),
    )
    monkeypatch.setenv("TEST_AGENT_PLANNING_MODEL_ID", "planner")
    monkeypatch.setenv("TEST_AGENT_ACT_MODEL_ID", "vision")
    monkeypatch.setenv("TEST_AGENT_JUDGE_MODEL_ID", "vision")
    monkeypatch.setenv("TEST_AGENT_HISTORY_MAX_TOKENS", "12000")
    monkeypatch.setenv("ADB_PATH", "custom-adb")
    monkeypatch.setenv("ADB_SERIAL", "device-1")

    settings_factory = cast(Callable[..., Settings], Settings)
    settings = settings_factory(_env_file=None)

    assert settings.debug is True
    assert settings.data_dir == PROJECT_ROOT / "config-test-data"
    assert [profile.id for profile in settings.llm_profiles] == ["planner", "vision"]
    assert settings.planning_model_id == "planner"
    assert settings.act_model_id == "vision"
    assert settings.judge_model_id == "vision"
    assert settings.agent_history_max_tokens == 12_000
    assert settings.adb_path == "custom-adb"
    assert settings.adb_serial == "device-1"
