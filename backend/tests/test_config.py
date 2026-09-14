from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from app.config import LLMProfileSettings, PROJECT_ROOT, Settings


settings_factory = cast(Callable[..., Settings], Settings)


def test_settings_load_model_parameters_from_toml_and_routes_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    model_config_path = tmp_path / "models.toml"
    model_config_path.write_text('''
[[profiles]]
id = "planner"
mode = "real"
base_url = "https://planner.invalid/v1"
api_key = "planner-secret"
model = "planning-model"
context_window_tokens = 16384
max_output_tokens = 1024

[[profiles]]
id = "vision"
mode = "real"
base_url = "https://vision.invalid/v1"
api_key = "vision-secret"
model = "vision-model"
context_window_tokens = 32768
max_output_tokens = 4096
characters_per_token = 2.0
tokens_per_image = 1500
context_safety_margin_tokens = 2048
temperature = 0.5
timeout_seconds = 90
''', encoding="utf-8")
    monkeypatch.setenv("DEBUG", "release")
    monkeypatch.setenv("TEST_AGENT_DEBUG", "true")
    monkeypatch.setenv("TEST_AGENT_DATA_DIR", "./config-test-data")
    monkeypatch.setenv("TEST_AGENT_MODEL_CONFIG_PATH", str(model_config_path))
    monkeypatch.setenv("TEST_AGENT_PLANNING_MODEL_ID", "planner")
    monkeypatch.setenv("TEST_AGENT_EXECUTION_MODEL_ID", "vision")
    settings = settings_factory(_env_file=None)
    assert settings.debug is True
    assert settings.data_dir == PROJECT_ROOT / "config-test-data"
    assert settings.planning_model_id == "planner"
    assert settings.execution_model_id == "vision"
    planner, vision = settings.llm_profiles
    assert planner == LLMProfileSettings(
        id="planner", mode="real", base_url="https://planner.invalid/v1",
        api_key="planner-secret", model="planning-model",
        context_window_tokens=16384, max_output_tokens=1024,
    )
    assert vision == LLMProfileSettings(
        id="vision", mode="real", base_url="https://vision.invalid/v1",
        api_key="vision-secret", model="vision-model",
        context_window_tokens=32768, max_output_tokens=4096,
        characters_per_token=2.0, tokens_per_image=1500,
        context_safety_margin_tokens=2048, temperature=0.5, timeout_seconds=90,
    )


def test_example_env_loads_relative_model_file_with_empty_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Resolve model paths from the repository even when launched elsewhere.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_AGENT_PLANNING_MODEL_ID", raising=False)
    monkeypatch.delenv("TEST_AGENT_EXECUTION_MODEL_ID", raising=False)
    settings = settings_factory(
        _env_file=PROJECT_ROOT / ".env.example",
        model_config_path=Path("models.example.toml"),
    )
    assert settings.model_config_path == PROJECT_ROOT / "models.example.toml"
    assert settings.planning_model_id is None
    assert settings.execution_model_id is None
    assert settings.llm_profiles == [LLMProfileSettings(id="default")]


@pytest.mark.parametrize("model_document, message", [
    ('profiles = []', "at least 1"),
    ('[[profiles]]\nid = "same"\n[[profiles]]\nid = "same"', "unique"),
    ('[[profiles]]\nid = "bad"\ncontext_window_tokens = 2048', "smaller than"),
    ('[[profiles]]\nid = "bad"\nunknown = 1', "Extra inputs"),
    ('planning_model_id = "default"\n[[profiles]]\nid = "default"', "Extra inputs"),
    ('', "Field required"),
    ('[[profiles]', "Expected"),
])
def test_invalid_model_file_prevents_startup(
    tmp_path: Path, model_document: str, message: str,
) -> None:
    model_config_path = tmp_path / "models.toml"
    model_config_path.write_text(model_document, encoding="utf-8")
    with pytest.raises(ValidationError, match=message):
        settings_factory(_env_file=None, model_config_path=model_config_path)


def test_missing_model_file_prevents_startup(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        settings_factory(_env_file=None, model_config_path=tmp_path / "missing.toml")


@pytest.mark.parametrize("route", ["planning_model_id", "execution_model_id"])
def test_model_routes_reject_unknown_ids(route: str) -> None:
    with pytest.raises(ValidationError, match="unknown model"):
        settings_factory(
            _env_file=None, model_config_path=PROJECT_ROOT / "models.example.toml",
            **{route: "missing"},
        )


def test_llm_profile_rejects_unknown_activity_markers_and_invalid_budget() -> None:
    with pytest.raises(ValidationError, match="activities"):
        LLMProfileSettings.model_validate(
            {"id": "vision", "activities": ["act", "judge"]}
        )
    with pytest.raises(ValidationError, match="smaller than"):
        LLMProfileSettings(
            id="invalid",
            context_window_tokens=2048,
            max_output_tokens=1024,
            context_safety_margin_tokens=1024,
        )
