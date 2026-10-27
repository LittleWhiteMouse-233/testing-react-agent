from pathlib import Path

import pytest

from app.domain.activity import AgentActivity
from app.domain.planning import TestTaskType as TaskType
from app.resources.prompts import PromptCatalog


def test_default_directory_loads_bundled_planning_and_task_prompts() -> None:
    catalog = PromptCatalog()

    planning_prompt = catalog.prompt_for_activity(AgentActivity.PLANNING)
    assert planning_prompt is not None
    assert planning_prompt.name == "planning"
    for task_type in TaskType:
        task_prompt = catalog.prompt_for_task_type(task_type)
        assert task_prompt is not None
        assert task_prompt.name == task_type.value


@pytest.mark.parametrize("task_type", list(TaskType))
@pytest.mark.parametrize("task_prompt_text", ["Task instructions", ""])
def test_task_prompt_overrides_execution_including_explicit_empty_content(
    tmp_path: Path, task_type: TaskType, task_prompt_text: str,
) -> None:
    (tmp_path / "execution.txt").write_text("Shared instructions", encoding="utf-8")
    (tmp_path / f"{task_type.value}.txt").write_text(task_prompt_text, encoding="utf-8")
    catalog = PromptCatalog(prompt_directory=tmp_path)

    prompt = catalog.prompt_for_task_type(task_type)
    assert prompt is not None
    assert prompt.name == task_type.value
    assert prompt.text == task_prompt_text
    assert catalog.prompt_fingerprints_by_task_type[task_type] == prompt.fingerprint


def test_missing_task_prompts_use_execution_and_its_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "execution.txt").write_text("abc", encoding="utf-8")
    catalog = PromptCatalog(prompt_directory=tmp_path)

    for task_type in TaskType:
        prompt = catalog.prompt_for_task_type(task_type)
        assert prompt is not None
        assert prompt.name == "execution"
        assert prompt.text == "abc"
    assert catalog.prompt_fingerprints_by_task_type == {
        TaskType.ACT: "ba7816bf8f01", TaskType.JUDGE: "ba7816bf8f01", TaskType.GOAL: "ba7816bf8f01",
    }


def test_missing_prompts_have_no_definition_or_fingerprint(tmp_path: Path) -> None:
    catalog = PromptCatalog(prompt_directory=tmp_path)

    assert all(catalog.prompt_for_activity(activity) is None for activity in AgentActivity)
    assert all(catalog.prompt_for_task_type(task_type) is None for task_type in TaskType)
    assert catalog.prompt_fingerprints_by_task_type == {TaskType.ACT: None, TaskType.JUDGE: None, TaskType.GOAL: None}


def test_explicit_empty_prompt_has_a_content_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "planning.txt").write_bytes(b"")
    catalog = PromptCatalog(prompt_directory=tmp_path)
    prompt = catalog.prompt_for_activity(AgentActivity.PLANNING)

    assert prompt is not None
    assert prompt.name == "planning"
    assert prompt.text == ""
    assert prompt.fingerprint == "e3b0c44298fc"


def test_invalid_prompt_encoding_is_reported(tmp_path: Path) -> None:
    (tmp_path / "planning.txt").write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        PromptCatalog(prompt_directory=tmp_path)
