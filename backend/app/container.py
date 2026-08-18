from __future__ import annotations

from app.artifacts import ArtifactStore
from app.llm import (
    ChatModelClient,
    ModelProvider,
    RealChatModelClient,
    ScriptedChatModelClient,
)
from app.device import DeviceProvider, FakeDeviceController, AdbDeviceController
from app.config import Settings
from app.event_stream import EventBus, EventWriter
from app.execution.active_runs import ActiveRunRegistry
from app.execution.executor import RunExecutor
from app.execution.run_service import RunService
from app.execution.task_agent import TaskAgentFactory
from app.persistence.db import build_engine, build_session_factory
from app.persistence.test_repository import SqlAlchemyTestRepository
from app.planning import PlanningGraph, PlanningService
from app.prompts import load_prompt_catalog
from app.reporting import ReportService
from app.tools import CatalogToolProvider


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings.db_url)
        self.sessions = build_session_factory(self.engine)
        self.event_bus = EventBus()
        self.events = EventWriter(self.sessions, self.event_bus)
        self.active_run_registry = ActiveRunRegistry()
        self.artifacts = ArtifactStore(
            settings.artifacts_dir,
            self.sessions,
        )
        self.devices: dict[str, DeviceProvider] = {"fake-tv": FakeDeviceController()}
        if settings.adb_serial:
            self.devices[settings.adb_serial] = AdbDeviceController(
                settings.adb_serial,
                settings.adb_path,
                settings.action_timeout_seconds,
            )
        self.tools = CatalogToolProvider(list(self.devices.values()))
        prompt_catalog = load_prompt_catalog()
        model_clients_by_id: dict[str, ChatModelClient] = {}
        for profile in settings.llm_profiles:
            if profile.mode == "real":
                model_client: ChatModelClient = RealChatModelClient(profile)
            elif profile.mode == "scripted":
                model_client = ScriptedChatModelClient(profile)
            else:
                raise ValueError(
                    f"Unsupported model profile mode: {profile.mode}"
                )
            model_clients_by_id[profile.id] = model_client
        self.model_provider = ModelProvider(
            model_clients_by_id,
            planning_model_id=settings.planning_model_id,
            act_model_id=settings.act_model_id,
            judge_model_id=settings.judge_model_id,
        )
        self.planning_graph = PlanningGraph(prompt_catalog.planner)
        self.repository = SqlAlchemyTestRepository(self.sessions, self.events)
        self.planning = PlanningService(
            repository=self.repository,
            planning_graph=self.planning_graph,
            model_provider=self.model_provider,
            devices=self.devices,
        )
        self.agent_factory = TaskAgentFactory(
            artifacts=self.artifacts,
            model_provider=self.model_provider,
            tool_provider=self.tools,
            devices=self.devices,
            act_prompt=prompt_catalog.act,
            judge_prompt=prompt_catalog.judge,
            action_timeout_seconds=settings.action_timeout_seconds,
            capture_max_attempts=settings.capture_max_attempts,
            model_call_max_attempts=settings.model_call_max_attempts,
            model_response_max_attempts=settings.model_response_max_attempts,
        )
        self.executor = RunExecutor(
            repository=self.repository,
            agent_factory=self.agent_factory,
            checkpoint_path=str(settings.checkpoints),
        )
        self.run_service = RunService(
            repository=self.repository,
            executor=self.executor,
            active_run_registry=self.active_run_registry,
            model_provider=self.model_provider,
            app_version=settings.app_version,
            act_prompt=prompt_catalog.act,
            judge_prompt=prompt_catalog.judge,
            devices=self.devices,
            tools=self.tools,
        )
        self.reports = ReportService(self.repository, self.artifacts)
