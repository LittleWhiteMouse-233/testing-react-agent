from __future__ import annotations

from app.llm import (
    ChatModelProvider,
    RealChatModelProvider,
    ScriptedChatModelProvider,
)
from app.device import DeviceController, FakeDeviceController, AdbDeviceController
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
from app.services.tools import StaticToolProvider


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
        self.tools = StaticToolProvider()
        self.devices: dict[str, DeviceController] = {"fake-tv": FakeDeviceController()}
        if settings.adb_serial:
            self.devices[settings.adb_serial] = AdbDeviceController(
                settings.adb_serial,
                settings.adb_path,
                settings.action_timeout_seconds,
            )
        self.model_provider: ChatModelProvider
        if settings.llm_mode == "real":
            self.model_provider = RealChatModelProvider(
                base_url=settings.llm_base_url or "",
                api_key=settings.llm_api_key or "",
                model=settings.llm_model,
                temperature=settings.llm_temperature,
                timeout_seconds=settings.llm_timeout_seconds,
                save_raw_response=settings.llm_save_raw_response,
            )
        else:
            self.model_provider = ScriptedChatModelProvider()
        self.planning_graph = PlanningGraph(self.model_provider)
        self.repository = SqlAlchemyExecutionRepository(self.sessions, self.events)
        self.agent_factory = TaskAgentFactory(
            repository=self.repository,
            journal=self.events,
            artifacts=self.artifacts,
            model_provider=self.model_provider,
            tool_provider=self.tools,
            registry=self.registry,
            devices=self.devices,
            action_timeout_seconds=settings.action_timeout_seconds,
            history_max_tokens=settings.agent_history_max_tokens,
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
            model_provider=self.model_provider,
            settings=settings,
            devices=self.devices,
            tools=self.tools,
        )
        self.reports = ReportService(self.sessions, self.artifacts)
