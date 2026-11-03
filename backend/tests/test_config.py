from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app import config
from config_support import app_config_values, write_app_config


def test_load_complete_toml_without_legacy_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_app_config(tmp_path, app_config_values(
        tmp_path, debug=True, cors_origins=["http://localhost:5173", "http://localhost:8000"],
        execution={"max_cycles_by_task_type": {"act": 40, "judge": 15, "goal": 20}},
    ))
    monkeypatch.setenv("TEST_AGENT_DEBUG", "false")
    monkeypatch.setenv("TEST_AGENT_ACT_MAX_CYCLES", "99")
    monkeypatch.setenv("TEST_AGENT_MODEL_CONFIG_PATH", str(tmp_path / "missing-models.toml"))
    settings = config.load_settings(config_path)
    assert settings.debug is True
    assert settings.cors_origins == ["http://localhost:5173", "http://localhost:8000"]
    assert settings.execution.max_cycles_by_task_type == {"act": 40, "judge": 15, "goal": 20}
    assert settings.llm.profiles[0].model == "deterministic"


@pytest.mark.parametrize("path_variable", [None, "", "chosen/config.toml"])
def test_file_selection_and_relative_paths_are_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_variable: str | None,
) -> None:
    project_root = tmp_path / "project"
    config_root = project_root / "chosen" if path_variable else project_root
    write_app_config(config_root, app_config_values(
        Path("runtime"), mcp_config_path="services/mcp.json", skills_dir="resources",
        checkpoint_path="checkpoints/graph.db",
    ))
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.chdir(tmp_path)
    if path_variable is None:
        monkeypatch.delenv("TEST_AGENT_CONFIG_PATH", raising=False)
    else:
        monkeypatch.setenv("TEST_AGENT_CONFIG_PATH", path_variable)
    settings = config.load_settings()
    assert settings.data_dir == project_root / "runtime"
    assert settings.mcp_config_path == project_root / "services/mcp.json"
    assert settings.skills_dir == project_root / "resources"
    assert settings.checkpoints == project_root / "checkpoints/graph.db"


def test_explicit_file_wins_over_environment_and_optional_paths_are_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENT_CONFIG_PATH", str(tmp_path / "missing.toml"))
    settings = config.load_settings(write_app_config(tmp_path))
    assert settings.db_url == f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    assert settings.checkpoints == tmp_path / "checkpoints.db"
    assert settings.artifacts_dir == tmp_path / "artifacts"
    assert settings.llm.planning_model_id == "default"
    assert settings.llm.execution_model_id == "default"
    assert not (tmp_path / "artifacts").exists()


def test_get_settings_caches_and_prepares_only_the_selected_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_app_config(tmp_path)
    monkeypatch.setenv("TEST_AGENT_CONFIG_PATH", str(config_path))
    config.get_settings.cache_clear()
    try:
        first = config.get_settings()
        config_path.unlink()
        assert config.get_settings() is first
        assert (tmp_path / "artifacts").is_dir()
    finally:
        config.get_settings.cache_clear()


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_missing_actual_file_never_uses_sample_or_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_selection: bool,
) -> None:
    write_app_config(tmp_path).rename(tmp_path / "config.example.toml")
    (tmp_path / ".env").write_text("TEST_AGENT_DEBUG=true\n", encoding="utf-8")
    (tmp_path / "models.toml").write_text('[[profiles]]\nid = "old"\n', encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TEST_AGENT_CONFIG_PATH", raising=False)
    if explicit_selection:
        monkeypatch.setenv("TEST_AGENT_CONFIG_PATH", "missing.toml")
    with pytest.raises(FileNotFoundError):
        config.load_settings()


@pytest.mark.parametrize("missing_field", ["debug", "execution", "llm"])
def test_necessary_parameters_have_no_python_defaults(tmp_path: Path, missing_field: str) -> None:
    configuration = app_config_values(tmp_path)
    configuration.pop(missing_field)
    with pytest.raises(ValidationError):
        config.load_settings(write_app_config(tmp_path, configuration))


@pytest.mark.parametrize("cycle_limits", [
    {}, {"act": 30, "judge": 10}, {"act": 30, "judge": 10, "other": 10},
    {"act": 0, "judge": 10, "goal": 10}, {"act": 30, "judge": 101, "goal": 10},
])
def test_cycle_limits_are_complete_and_bounded(tmp_path: Path, cycle_limits: dict[str, int]) -> None:
    configuration = app_config_values(tmp_path, execution={"max_cycles_by_task_type": cycle_limits})
    with pytest.raises(ValidationError, match="max_cycles_by_task_type"):
        config.load_settings(write_app_config(tmp_path, configuration))


@pytest.mark.parametrize("parameter, value", [
    ("goal_max_failures", 0), ("image_history_rounds", 0),
    ("tool_call_timeout_seconds", 0), ("model_call_max_attempts", 0),
    ("model_response_max_attempts", 11),
])
def test_execution_parameters_reject_invalid_values(tmp_path: Path, parameter: str, value: int) -> None:
    configuration = app_config_values(tmp_path, execution={parameter: value})
    with pytest.raises(ValidationError, match=parameter):
        config.load_settings(write_app_config(tmp_path, configuration))


@pytest.mark.parametrize("route", ["planning_model_id", "execution_model_id"])
def test_model_routes_use_ids_from_the_same_file(tmp_path: Path, route: str) -> None:
    configuration = app_config_values(tmp_path, llm={route: "default"})
    assert getattr(config.load_settings(write_app_config(tmp_path, configuration)).llm, route) == "default"
    configuration["llm"][route] = "missing"
    with pytest.raises(ValidationError, match="unknown model"):
        config.load_settings(write_app_config(tmp_path, configuration))


def test_model_identity_is_normalized_consistently_for_profiles_and_routes(tmp_path: Path) -> None:
    configuration = app_config_values(tmp_path, llm={"planning_model_id": " default ", "execution_model_id": "default"})
    configuration["llm"]["profiles"][0]["id"] = " default "
    settings = config.load_settings(write_app_config(tmp_path, configuration))
    assert settings.llm.profiles[0].id == settings.llm.planning_model_id == settings.llm.execution_model_id == "default"


def test_duplicate_model_identity_is_rejected_after_normalization(tmp_path: Path) -> None:
    configuration = app_config_values(tmp_path)
    configuration["llm"]["profiles"].append({**configuration["llm"]["profiles"][0], "id": " default "})
    with pytest.raises(ValidationError, match="unique"):
        config.load_settings(write_app_config(tmp_path, configuration))


@pytest.mark.parametrize("invalid_profile", ["missing_parameter", "unknown_parameter", "capacity", "credentials", "duplicate", "empty"])
def test_invalid_model_configuration_is_rejected_without_exposing_secrets(
    tmp_path: Path, invalid_profile: str,
) -> None:
    configuration = app_config_values(tmp_path)
    profile = configuration["llm"]["profiles"][0]
    profile["api_key"] = "never-display-this-secret"
    if invalid_profile == "missing_parameter":
        profile.pop("temperature")
    elif invalid_profile == "unknown_parameter":
        profile["activities"] = ["act"]
    elif invalid_profile == "capacity":
        profile["context_window_tokens"] = 2048
    elif invalid_profile == "credentials":
        profile["mode"] = "real"
    elif invalid_profile == "duplicate":
        configuration["llm"]["profiles"].append(dict(profile))
    else:
        configuration["llm"]["profiles"] = []
    with pytest.raises(ValidationError) as failure:
        config.load_settings(write_app_config(tmp_path, configuration))
    assert "never-display-this-secret" not in str(failure.value)


def test_unknown_host_parameter_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        config.load_settings(write_app_config(tmp_path, app_config_values(tmp_path, typo=1)))


def test_invalid_toml_stops_loading_without_echoing_its_contents(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('api_key = "never-display-this-secret" invalid\n', encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        config.load_settings(config_path)
    assert "never-display-this-secret" not in str(failure.value)


def test_importing_application_factory_does_not_read_personal_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from app.main import create_app; print(callable(create_app))"],
        cwd=config.PROJECT_ROOT / "backend", capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "TEST_AGENT_CONFIG_PATH": str(tmp_path / "missing.toml"),
             "TEST_AGENT_MODEL_CONFIG_PATH": str(tmp_path / "missing-models.toml")},
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "True"


def test_openapi_export_uses_selected_actual_configuration_without_starting_services(tmp_path: Path) -> None:
    configuration = app_config_values(
        tmp_path, app_name="Export configuration", data_dir=(tmp_path / "unused-data").as_posix(),
    )
    configuration["llm"]["profiles"][0]["api_key"] = "never-export-this-secret"
    config_path = write_app_config(tmp_path, configuration)
    output_path = tmp_path / "openapi.json"
    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py", str(output_path)],
        cwd=config.PROJECT_ROOT / "backend", capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "TEST_AGENT_CONFIG_PATH": str(config_path)}, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exported_text = output_path.read_text(encoding="utf-8")
    assert json.loads(exported_text)["info"]["title"] == "Export configuration"
    assert "never-export-this-secret" not in exported_text
    assert not (tmp_path / "unused-data").exists()
