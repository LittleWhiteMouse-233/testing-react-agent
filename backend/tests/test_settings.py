"""Settings commands preserve atomic configuration and working snapshots."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import tomllib

import pytest
from pydantic import ValidationError

from app import config
from app.domain.resources import LLMProfileSettings
from app.test_case_lock import TestCaseLock as CaseLock
from config_support import app_config_values, write_app_config


def settings_service(root: Path):
    configuration = app_config_values(root, llm={
        "planning_model_id": "default", "execution_model_id": "default",
    })
    configuration["llm"]["profiles"][0].update(
        mode="real", base_url="https://model.invalid/v1", api_key="old-test-secret",
    )
    config_path = write_app_config(root, configuration)
    case_lock = CaseLock()
    service = config.SettingsService(config.get_settings(config_path), case_lock)
    return service, case_lock, config_path


@pytest.mark.parametrize("token_update", [{}, {"api_key": None}, {"api_key": ""}])
async def test_save_preserves_credentials_and_publishes_only_after_file_commit(
    tmp_path: Path, token_update: dict[str, str | None],
) -> None:
    service, _, config_path = settings_service(tmp_path)
    original = service.current
    version = service.version
    draft = original.model_dump(mode="json")
    draft["llm"]["profiles"][0].update(token_update)
    draft["execution"]["goal_max_failures"] = 7
    await service.update(config.SettingsContent[LLMProfileSettings].model_validate(draft), version=version)
    assert service.current.execution.goal_max_failures == 7
    assert config.get_settings(config_path).execution.goal_max_failures == 7
    assert service.current.llm.profiles[0].api_key == "old-test-secret"
    assert config.get_settings(config_path).llm.profiles[0].api_key == "old-test-secret"
    assert original.execution.goal_max_failures == 10
    assert service.version != version
    assert service.restart_required is False
    with pytest.raises(config.SettingsVersionConflict):
        await service.update(config.SettingsContent[LLMProfileSettings].model_validate(draft), version=version)


async def test_repeated_saves_keep_the_bound_file_after_environment_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_app_config(tmp_path / "selected")
    other_config_path = write_app_config(tmp_path / "other")
    other_configuration = other_config_path.read_bytes()
    service = config.SettingsService(config.get_settings(config_path), CaseLock())
    original = service.current
    monkeypatch.setenv("TEST_AGENT_CONFIG_PATH", str(other_config_path))

    for failure_limit in (7, 8):
        draft = service.current.model_dump()
        draft["execution"]["goal_max_failures"] = failure_limit
        await service.update(config.SettingsContent[LLMProfileSettings].model_validate(draft), version=service.version)
        assert service.current.config_path == config_path
        assert config.get_settings(config_path).execution.goal_max_failures == failure_limit
        assert "config_path" not in tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert service.restart_required is False

    assert original.execution.goal_max_failures == 10
    assert original.config_path == config_path
    assert other_config_path.read_bytes() == other_configuration


async def test_startup_changes_remain_flagged_after_reverting_and_reset_on_restart(tmp_path: Path) -> None:
    service, _, config_path = settings_service(tmp_path)
    for app_name in ("Changed app", "Android TV Test Agent"):
        draft = service.current.model_dump(mode="json")
        draft["app_name"] = app_name
        await service.update(config.SettingsContent[LLMProfileSettings].model_validate(draft), version=service.version)
        assert service.restart_required is True
    restarted = config.SettingsService(config.get_settings(config_path), CaseLock())
    assert restarted.restart_required is False
    assert restarted.version != service.version


@pytest.mark.parametrize("occupation", ["pre_planning", "pre_running"])
async def test_any_case_preparation_rejects_settings_save(tmp_path: Path, occupation) -> None:
    service, case_lock, config_path = settings_service(tmp_path)
    original_file = config_path.read_bytes()
    with case_lock.hold("another-case", occupation):
        with pytest.raises(config.SettingsBusy):
            await service.update(config.SettingsContent[LLMProfileSettings].model_validate(
                service.current.model_dump()), version=service.version)
    assert config_path.read_bytes() == original_file


async def test_replacement_failure_keeps_memory_version_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, config_path = settings_service(tmp_path)
    original_file, original_version = config_path.read_bytes(), service.version
    draft = service.current.model_dump()
    draft["debug"] = True

    def denied_replace(*args):
        raise PermissionError("file is read-only")

    monkeypatch.setattr(config.os, "replace", denied_replace)
    with pytest.raises(OSError):
        await service.update(config.SettingsContent[LLMProfileSettings].model_validate(draft), version=original_version)
    assert config_path.read_bytes() == original_file
    assert service.version == original_version
    assert service.current.debug is False
    assert service.current.config_path == config_path
    assert service.restart_required is False
    assert list(tmp_path.glob("*.tmp")) == []
    service.ensure_available()


async def test_cancelled_caller_does_not_release_an_ongoing_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, config_path = settings_service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_write = service._write_temporary_file

    def blocked_write(contents: str) -> Path:
        entered.set()
        assert release.wait(5)
        return original_write(contents)

    monkeypatch.setattr(service, "_write_temporary_file", blocked_write)
    draft = service.current.model_dump()
    draft["execution"]["goal_max_failures"] = 9
    command = asyncio.create_task(service.update(
        config.SettingsContent[LLMProfileSettings].model_validate(draft), version=service.version))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        command.cancel()
        await asyncio.sleep(0)
        with pytest.raises(config.SettingsBusy):
            service.ensure_available()
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)
        await service.shutdown()
    assert config.get_settings(config_path).execution.goal_max_failures == 9


async def test_new_real_model_requires_new_credentials_before_commit(tmp_path: Path) -> None:
    service, _, config_path = settings_service(tmp_path)
    original_file = config_path.read_bytes()
    draft = service.current.model_dump()
    draft["llm"]["profiles"][0].update(id="new-model", api_key=None)
    draft["llm"].update(planning_model_id="new-model", execution_model_id="new-model")
    with pytest.raises(ValidationError):
        await service.update(config.SettingsContent[LLMProfileSettings].model_validate(draft), version=service.version)
    assert config_path.read_bytes() == original_file
