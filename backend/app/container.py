from __future__ import annotations

from app.llm import (
    ChatModelProvider,
    ModelRegistry,
    RealChatModelProvider,
    ScriptedChatModelProvider,
)
from app.device import DeviceProvider, FakeDeviceController, AdbDeviceController
from app.config import Settings
from app.execution.executor import RunExecutor
from app.graph.planning import PlanningGraph
from app.graph.task_agent import TaskAgentFactory
from app.persistence.db import build_engine, build_session_factory
from app.persistence.execution_repository import SqlAlchemyExecutionRepository
from app.services.artifacts import ArtifactStore
from app.services.event_bus import EventBus
from app.services.events import EventWriter
from app.services.registry import RunRegistry
from app.services.reporting import ReportService
from app.services.run_service import RunService
from app.tools import CatalogToolProvider


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings.db_url)
        self.sessions = build_session_factory(self.engine)
        self.event_bus = EventBus()
        self.events = EventWriter(self.sessions, self.event_bus)
        self.registry = RunRegistry()
        self.artifacts = ArtifactStore(
            settings.artifacts_dir,
            self.sessions,
            self.events,
        )
        self.devices: dict[str, DeviceProvider] = {"fake-tv": FakeDeviceController()}
        if settings.adb_serial:
            self.devices[settings.adb_serial] = AdbDeviceController(
                settings.adb_serial,
                settings.adb_path,
                settings.action_timeout_seconds,
            )
        self.tools = CatalogToolProvider(list(self.devices.values()))
        self.models: dict[str, ChatModelProvider] = {}
        for profile in settings.llm_profiles:
            if profile.mode == "real":
                provider: ChatModelProvider = RealChatModelProvider(
                    model_id=profile.id,
                    base_url=profile.base_url or "",
                    api_key=profile.api_key or "",
                    model=profile.model,
                    temperature=profile.temperature,
                    timeout_seconds=profile.timeout_seconds,
                    save_raw_response=profile.save_raw_response,
                )
            elif profile.mode == "scripted":
                provider = ScriptedChatModelProvider(
                    model_id=profile.id,
                    timeout_seconds=profile.timeout_seconds,
                )
            else:
                raise ValueError(
                    f"Unsupported model profile mode: {profile.mode}"
                )
            self.models[profile.id] = provider
        self.model_registry = ModelRegistry(
            self.models,
            planning_model_id=settings.planning_model_id,
            act_model_id=settings.act_model_id,
            judge_model_id=settings.judge_model_id,
        )
        self.planning_graph = PlanningGraph(self.model_registry)
        self.repository = SqlAlchemyExecutionRepository(self.sessions, self.events)
        self.agent_factory = TaskAgentFactory(
            repository=self.repository,
            journal=self.events,
            artifacts=self.artifacts,
            model_registry=self.model_registry,
            tool_provider=self.tools,
            registry=self.registry,
            devices=self.devices,
            history_max_tokens=settings.agent_history_max_tokens,
            action_timeout_seconds=settings.action_timeout_seconds,
            tool_timeout_max_attempts=settings.tool_timeout_max_attempts,
            tool_call_max_attempts=settings.tool_call_max_attempts,
        )
        self.executor = RunExecutor(
            repository=self.repository,
            journal=self.events,
            agent_factory=self.agent_factory,
            registry=self.registry,
            checkpoint_path=str(settings.checkpoints),
        )
        self.run_service = RunService(
            sessions=self.sessions,
            repository=self.repository,
            executor=self.executor,
            registry=self.registry,
            model_registry=self.model_registry,
            settings=settings,
            devices=self.devices,
            tools=self.tools,
        )
        self.reports = ReportService(self.sessions, self.artifacts)
