from __future__ import annotations

from app.api import repository as api_repository

import ast
import asyncio
import json
import tempfile
from pathlib import Path
from inspect import signature

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from sqlalchemy import inspect

from app.config import PROJECT_ROOT, Settings
from app.domain.execution import (
    ArtifactType,
    MessageAppendedEvent,
    ReasonCode,
    RunHumanMessage,
    RunTextBlock,
    TaskRunResult,
    TaskRunStatus,
    TestRun as DomainTestRun,
    TestRunEnvironmentSnapshot as RunEnvironmentSnapshot,
)
from app.domain.planning import (
    TestCaseContent as CaseContent,
    TestPlan as DomainTestPlan,
    TestPlanContent as PlanContent,
    TestPlanOrigin as PlanOrigin,
    TestPlanPlanningContext as PlanningContext,
    TestTaskDefinition as TaskDefinition,
    TestTaskType as TaskType,
)
from app.domain.resources.llm import LLMProfileSnapshot
from app.domain.resources.tools import ToolCatalogSnapshot
from app.domain.resources.static_resources import ResourceCatalogSnapshot
from app.event_stream import EventBus, RunEventWriter, project_run_message
from app.execution.task_agent import TaskAgentGraphState
from app.execution import RunExecutor, RunService, TaskAgentFactory
from app.planning import PlanningGraph
from app.main import create_app
from app.persistence.adapters import (
    dump_planning_context,
    dump_string_list,
    dump_task_run_result,
    dump_test_run_environment_snapshot,
    dump_run_event,
    load_planning_context,
    load_run_event,
    load_string_list,
    load_task_run_result,
    load_test_run_environment_snapshot,
)
from app.persistence.db import (
    build_engine,
    build_session_factory,
    init_database,
)
from app.persistence.run_repository import (
    ActiveRunExists,
    SqlAlchemyRunRepository,
)


RUN_ID = "11111111-1111-4111-8111-111111111111"
TASK_RUN_ID = "22222222-2222-4222-8222-222222222222"
ARTIFACT_ID = "33333333-3333-4333-8333-333333333333"


def model_snapshot() -> LLMProfileSnapshot:
    return LLMProfileSnapshot(
        profile_id="default",
        provider="scripted",
        model="deterministic",
        base_url=None,
        temperature=0,
        timeout_seconds=60,
        context_window_tokens=32_768,
        max_output_tokens=2_048,
        characters_per_token=1.5,
        tokens_per_image=1_024,
        context_safety_margin_tokens=1_024,
    )


def run_environment_snapshot() -> RunEnvironmentSnapshot:
    return RunEnvironmentSnapshot(
        tool_catalog=ToolCatalogSnapshot(tools=[]),
        resource_catalog=ResourceCatalogSnapshot(),
        execution_model=model_snapshot(),
        max_cycles_by_task_type={TaskType.ACT: 30, TaskType.JUDGE: 10},
        prompt_fingerprints_by_task_type={TaskType.ACT: "act-v1", TaskType.JUDGE: "judge-v1"},
        app_version="0.1.0",
        execution_protocol_version="7",
    )


def plan_draft(task_count: int = 1) -> PlanContent[TaskDefinition]:
    return PlanContent[TaskDefinition](
        title="Plan",
        setup_steps=[],
        assumptions=[],
        tasks=[
            TaskDefinition(
                type=TaskType.JUDGE,
                title=f"Task {index}",
                goal="Verify the screen",
                success_criteria=["The expected screen is visible"],
            )
            for index in range(task_count)
        ],
    )


def planning_context() -> PlanningContext:
    return PlanningContext(
        test_case_content=CaseContent(name="Case", source_text="Check TV"),
        planning_model=model_snapshot(),
        planning_prompt_fingerprint="planning-fingerprint",
    )


def test_every_business_json_uses_the_central_adapter() -> None:
    context = planning_context()
    assert load_planning_context(dump_planning_context(context)) == context
    strings = ["first", "second"]
    assert load_string_list(dump_string_list(strings)) == strings
    snapshot = run_environment_snapshot()
    assert load_test_run_environment_snapshot(dump_test_run_environment_snapshot(snapshot)) == snapshot
    result = TaskRunResult(
        reason_code=ReasonCode.COMPLETED,
        summary="done",
    )
    assert load_task_run_result(dump_task_run_result(result)) == result
    with pytest.raises(ValidationError):
        load_test_run_environment_snapshot({"device_environment": {}})

    event = MessageAppendedEvent(
        test_run_id=RUN_ID,
        task_run_id=TASK_RUN_ID,
        message=RunHumanMessage(
            message_id="message-1",
            content=[RunTextBlock(text="raw")],
        ),
    )
    event_type, payload = dump_run_event(event)
    assert load_run_event(
        event_type=event_type,
        test_run_id=RUN_ID,
        task_run_id=TASK_RUN_ID,
        payload=payload,
    ) == event
    with pytest.raises(ValidationError):
        load_run_event(
            event_type="observation.captured",
            test_run_id=RUN_ID,
            task_run_id=TASK_RUN_ID,
            payload={},
        )
    with pytest.raises(ValueError, match="reserved columns"):
        load_run_event(
            event_type=event_type,
            test_run_id=RUN_ID,
            task_run_id=TASK_RUN_ID,
            payload={"test_run_id": "66666666-6666-4666-8666-666666666666"},
        )

    invalid_snapshot = dump_test_run_environment_snapshot(snapshot)
    invalid_snapshot["execution_protocol_version"] = "1"
    with pytest.raises(ValidationError):
        load_test_run_environment_snapshot(invalid_snapshot)


@pytest.mark.parametrize("field_name,configuration", [
    ("max_cycles_by_task_type", {}),
    ("max_cycles_by_task_type", {"act": 30}),
    ("max_cycles_by_task_type", {"act": 30, "unknown": 10}),
    ("max_cycles_by_task_type", {"act": 0, "judge": 10}),
    ("max_cycles_by_task_type", {"act": 30, "judge": 101}),
    ("prompt_fingerprints_by_task_type", {}),
    ("prompt_fingerprints_by_task_type", {"judge": "judge-v1"}),
    ("prompt_fingerprints_by_task_type", {"unknown": "act-v1", "judge": "judge-v1"}),
    ("prompt_fingerprints_by_task_type", {"act": "", "judge": "judge-v1"}),
])
def test_run_environment_rejects_incomplete_or_invalid_task_type_configuration(
    field_name: str, configuration: dict[str, int | str],
) -> None:
    snapshot = dump_test_run_environment_snapshot(run_environment_snapshot())
    snapshot[field_name] = configuration
    with pytest.raises(ValidationError):
        load_test_run_environment_snapshot(snapshot)


def test_public_message_projection_never_serializes_image_base64() -> None:
    message = HumanMessage(
        id="message-1",
        content=[
            {"type": "text", "text": "original text"},
            {
                "type": "image",
                "base64": "very-large-base64",
                "mime_type": "image/png",
                "id": ARTIFACT_ID,
            },
        ],
    )
    projected = project_run_message(message)
    dumped = json.dumps(projected.model_dump(mode="json"))
    assert "original text" in dumped
    assert ARTIFACT_ID in dumped
    assert "very-large-base64" not in dumped

    ai = project_run_message(
        AIMessage(
            id="message-2",
            content="unaltered",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "device_wait",
                    "args": {"duration_ms": 100},
                    "type": "tool_call",
                }
            ],
        )
    )
    assert ai.content[0].text == "unaltered"  # type: ignore[union-attr]
    assert ai.tool_calls[0].arguments == {"duration_ms": 100}  # type: ignore[union-attr]

    malformed = project_run_message(
        AIMessage(
            id="message-3",
            content="",
            invalid_tool_calls=[
                {
                    "id": "bad-call-1",
                    "name": "device_wait",
                    "args": "{bad-json",
                    "error": "invalid JSON",
                    "type": "invalid_tool_call",
                }
            ],
        )
    )
    assert malformed.invalid_tool_calls[0].arguments == "{bad-json"  # type: ignore[union-attr]
    assert malformed.invalid_tool_calls[0].error == "invalid JSON"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_source", ["orm", "alembic"])
async def test_fresh_schema_has_only_canonical_tables_and_constraints(
    schema_source: str,
) -> None:
    import os
    import subprocess
    import sys
    from app.artifacts import ArtifactStore

    with tempfile.TemporaryDirectory(
        dir=PROJECT_ROOT / "data", ignore_cleanup_errors=True
    ) as directory:
        database = Path(directory) / "schema.db"
        engine = build_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
        if schema_source == "orm":
            await init_database(engine)
        else:
            migrated = await asyncio.to_thread(subprocess.run,
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=PROJECT_ROOT / "backend", capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "TEST_AGENT_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}"})
            assert migrated.returncode == 0, migrated.stdout + migrated.stderr

        sessions = build_session_factory(engine)
        repository = SqlAlchemyRunRepository(sessions, RunEventWriter(sessions, EventBus()))
        test_case = await api_repository.create_test_case(sessions, planning_context().test_case_content)
        plan = await repository.create_plan(test_case_id=test_case.id, draft=plan_draft(),
                                           planning_context=planning_context(), origin=PlanOrigin.PLANNING)
        run = await repository.create_test_run(test_plan_id=plan.id, environment=run_environment_snapshot())
        await repository.start_run(run.id)
        task = await repository.start_task(run.id, plan.content.tasks[0])
        artifacts = ArtifactStore(Path(directory) / "artifacts", sessions)
        image = await artifacts.save_image(task_run_id=task.id, content=b"image bytes", mime_type="image/png")
        assert image.type == ArtifactType.IMAGE and image.task_run_id == task.id
        exported = await artifacts.save_export(test_run_id=run.id, artifact_type=ArtifactType.JSON_EXPORT,
            content=b"{}", mime_type="application/json", extension="json")
        assert exported.test_run_id == run.id

        async with engine.connect() as connection:
            tables, active_index_sql, artifact_checks = await connection.run_sync(
                lambda sync: (
                    set(inspect(sync).get_table_names()),
                    sync.exec_driver_sql(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'index' AND name = 'uq_single_active_run'"
                    ).scalar_one(),
                    inspect(sync).get_check_constraints("artifacts"),
                )
            )
        await engine.dispose()
    assert tables - {"alembic_version"} == {
        "test_cases",
        "test_plans",
        "test_tasks",
        "test_runs",
        "task_runs",
        "run_events",
        "artifacts",
    }
    assert "CREATE UNIQUE INDEX" in active_index_sql
    assert "WHERE status IN ('pending', 'running')" in active_index_sql
    assert {item["name"] for item in artifact_checks} >= {
        "ck_artifact_owner_xor",
        "ck_artifact_type_owner",
    }


@pytest.mark.asyncio
async def test_latest_plan_and_single_active_run_are_serialized() -> None:
    with tempfile.TemporaryDirectory(
        dir=PROJECT_ROOT / "data", ignore_cleanup_errors=True
    ) as directory:
        database = Path(directory) / "concurrency.db"
        engine = build_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
        await init_database(engine)
        sessions = build_session_factory(engine)
        repository = SqlAlchemyRunRepository(
            sessions, RunEventWriter(sessions, EventBus())
        )
        generated_case = await api_repository.create_test_case(repository.sessions,
            CaseContent(name="Concurrent generation", source_text="Check TV")
        )
        generated: list[object] = list(
            await asyncio.gather(
                *(
                    repository.create_plan(
                        test_case_id=generated_case.id,
                        draft=plan_draft(),
                        planning_context=planning_context(),
                        origin=PlanOrigin.PLANNING,
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
        )
        generated_plans = sorted(
            (item for item in generated if isinstance(item, DomainTestPlan)),
            key=lambda item: item.version_number,
        )
        assert [item.version_number for item in generated_plans] == [1]
        assert generated_plans[0].origin == PlanOrigin.PLANNING
        assert sum(isinstance(item, ValueError) for item in generated) == 1

        test_case = await api_repository.create_test_case(repository.sessions, planning_context().test_case_content)
        initial = await repository.create_plan(
            test_case_id=test_case.id,
            draft=plan_draft(),
            planning_context=planning_context(),
            origin=PlanOrigin.PLANNING,
        )

        revisions: list[object] = list(
            await asyncio.gather(
                *(
                    repository.create_plan(
                        test_case_id=test_case.id,
                        draft=plan_draft(),
                        planning_context=planning_context(),
                        origin=PlanOrigin.MANUAL_REVISION,
                        derived_from_plan_id=initial.id,
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
        )
        created = [item for item in revisions if isinstance(item, DomainTestPlan)]
        rejected = [item for item in revisions if isinstance(item, ValueError)]
        assert len(created) == 1
        assert len(rejected) == 1
        assert created[0].version_number == 2

        runs: list[object] = list(
            await asyncio.gather(
                *(
                    repository.create_test_run(
                        test_plan_id=created[0].id,
                        environment=run_environment_snapshot(),
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
        )
        assert sum(isinstance(item, DomainTestRun) for item in runs) == 1
        assert sum(isinstance(item, ActiveRunExists) for item in runs) == 1
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [
    (TaskRunStatus.PASSED, ReasonCode.COMPLETED),
    (TaskRunStatus.FAILED, ReasonCode.ASSERTION_FAILED),
    (TaskRunStatus.BLOCKED, ReasonCode.AGENT_BLOCKED),
])
async def test_task_results_persist_observations_without_image_associations(
    tmp_path: Path, status: TaskRunStatus, reason: ReasonCode,
) -> None:
    engine = build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'results.db').as_posix()}")
    await init_database(engine)
    sessions = build_session_factory(engine)
    repository = SqlAlchemyRunRepository(sessions, RunEventWriter(sessions, EventBus()))
    test_case = await api_repository.create_test_case(sessions, planning_context().test_case_content)
    plan = await repository.create_plan(test_case_id=test_case.id, draft=plan_draft(),
                                       planning_context=planning_context(), origin=PlanOrigin.PLANNING)
    run = await repository.create_test_run(test_plan_id=plan.id, environment=run_environment_snapshot())
    await repository.start_run(run.id)
    task = await repository.start_task(run.id, plan.content.tasks[0])
    result = TaskRunResult(reason_code=reason, summary="Observed target state and compared it with the success criteria")
    await repository.finish_task(task.id, status=status, result=result, cycle_count=1)
    saved = (await repository.list_task_runs(run.id))[0]
    assert saved.status == status and saved.result == result
    assert (await api_repository.read_test_run_report(sessions, run.id)).artifacts == []
    await engine.dispose()

def test_domain_has_no_framework_or_integration_dependencies() -> None:
    domain_root = Path(__file__).resolve().parents[1] / "app" / "domain"
    banned = ("fastapi", "sqlalchemy", "langchain", "langgraph", "app.device", "app.llm")
    violations: list[str] = []
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(banned):
                    violations.append(f"{path.name}: {name}")
    assert violations == []


def test_domain_modules_and_public_structures_have_semantic_docstrings() -> None:
    domain_root = Path(__file__).resolve().parents[1] / "app" / "domain"
    missing: list[str] = []
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        if ast.get_docstring(tree) is None:
            missing.append(f"{path.relative_to(domain_root)}: module")
        for node in tree.body:
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
                and ast.get_docstring(node) is None
            ):
                missing.append(
                    f"{path.relative_to(domain_root)}:{node.lineno} {node.name}"
                )
    assert missing == []


def test_process_boundaries_do_not_restore_obsolete_dependency_ownership() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    assert not list((app_root / "services").glob("*.py"))
    assert not list((app_root / "graph").glob("*.py"))
    assert not (app_root / "execution" / "ports.py").exists()
    assert not (app_root / "domain" / "test_cases.py").exists()
    assert not (app_root / "llm" / "snapshots.py").exists()

    run_service_parameters = signature(RunService).parameters
    assert "app_version" in run_service_parameters
    assert "settings" not in run_service_parameters
    assert "active_run_registry" not in run_service_parameters
    assert "registry" not in signature(RunExecutor).parameters
    assert "registry" not in signature(TaskAgentFactory).parameters
    assert set(signature(PlanningGraph).parameters) == {"prompt_definition"}
    assert not hasattr(SqlAlchemyRunRepository, "load_snapshot")
    assert not hasattr(SqlAlchemyRunRepository, "get_report")


def test_graph_state_and_event_vocabulary_have_no_parallel_protocol() -> None:
    assert set(TaskAgentGraphState.__annotations__) == {
        "messages",
        "cycle_count",
        "model_response_attempt",
        "completion",
        "route",
    }
    repository_root = Path(__file__).resolve().parents[2]
    source_roots = [
        repository_root / "backend" / "app",
        repository_root / "frontend" / "src",
    ]
    forbidden = {
        "AIMessageChunk",
        "ObservationCapturedEvent",
        "AgentActionSelected",
        "ToolFinishedEvent",
        "pending_invocation",
        'stream_mode="messages"',
    }
    matches: list[str] = []
    for root in source_roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or path.name == "schema.d.ts":
                continue
            text = path.read_text("utf-8")
            for value in forbidden:
                if value in text:
                    matches.append(f"{path.relative_to(repository_root)}: {value}")
    assert matches == []


def test_checked_in_openapi_is_current() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    checked_in = json.loads(
        (repository_root / "frontend" / "src" / "api" / "openapi.json").read_text(
            "utf-8"
        )
    )
    generated = create_app(Settings(
        model_config_path=repository_root / "models.example.toml",
        planning_model_id=None,
        execution_model_id=None,
        data_dir=repository_root / "data" / "openapi-test",
    )).openapi()
    assert checked_in == generated
    schema_names = set(checked_in["components"]["schemas"])
    assert "ApiError" in schema_names
    assert not {
        "PlanRevision",
        "TaskExecutionResponse",
        "RunResponse",
        "ObservationCapturedEvent",
        "ToolFinishedEvent",
    } & schema_names
    schemas = checked_in["components"]["schemas"]
    assert {"verdict", "started_at", "finished_at"} <= set(
        schemas["TestRun"]["required"]
    )
    assert {"result", "started_at", "finished_at"} <= set(
        schemas["TaskRun"]["required"]
    )
    assert {"test_run_id", "task_run_id"} <= set(
        schemas["Artifact"]["required"]
    )

    paths = checked_in["paths"]
    cancel_responses = paths["/api/runs/{test_run_id}/cancel"]["post"]["responses"]
    assert cancel_responses["202"] == {
        "description": "Cancellation request accepted"
    }

    stream_operation = paths["/api/runs/{test_run_id}/stream"]["get"]
    assert set(stream_operation["responses"]["200"]["content"]) == {
        "text/event-stream"
    }
    assert any(
        parameter["in"] == "header" and parameter["name"] == "Last-Event-ID"
        for parameter in stream_operation["parameters"]
    )

    artifact_content = paths["/api/artifacts/{artifact_id}"]["get"]["responses"][
        "200"
    ]["content"]
    assert set(artifact_content) == {
        "image/png",
        "application/json",
        "text/html",
    }

    assert "/api/devices" not in paths
    assert "502" not in paths["/api/runs"]["get"]["responses"]
    assert "409" in paths["/api/runs"]["post"]["responses"]
    assert "502" in paths["/api/test-cases/{test_case_id}/plans"]["post"][
        "responses"
    ]
    for operations in paths.values():
        for operation in operations.values():
            for status_code, response in operation["responses"].items():
                if status_code in {"422", "500"}:
                    assert response["content"]["application/json"]["schema"] == {
                        "$ref": "#/components/schemas/ApiError"
                    }
