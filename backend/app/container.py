from __future__ import annotations

from app.llm import LLMProvider, ScriptedLLMProvider, RealLLMProvider
from app.device import DeviceController, FakeDeviceController, AdbDeviceController
from app.config import Settings
from app.graph.executor import AgentGraphExecutor
from app.persistence.db import build_engine, build_session_factory
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
        self.artifacts = ArtifactStore(settings.artifacts_dir, self.sessions)
        self.tools = StaticToolProvider()
        self.devices: dict[str, DeviceController] = {"fake-tv": FakeDeviceController()}
        if settings.adb_serial:
            self.devices[settings.adb_serial] = AdbDeviceController(
                settings.adb_serial,
                settings.adb_path,
                settings.action_timeout_seconds,
            )
        self.llm: LLMProvider
        if settings.llm_mode == "real":
            self.llm = RealLLMProvider(
                base_url=settings.llm_base_url or "",
                api_key=settings.llm_api_key or "",
                model=settings.llm_model,
                temperature=settings.llm_temperature,
                timeout_seconds=settings.llm_timeout_seconds,
                save_raw_response=settings.llm_save_raw_response,
            )
        else:
            self.llm = ScriptedLLMProvider()
        self.executor = AgentGraphExecutor(
            sessions=self.sessions,
            events=self.events,
            artifacts=self.artifacts,
            llm=self.llm,
            tools=self.tools,
            registry=self.registry,
            devices=self.devices,
            checkpoint_path=str(settings.checkpoints),
            action_timeout_seconds=settings.action_timeout_seconds,
        )
        self.run_service = RunService(
            sessions=self.sessions,
            executor=self.executor,
            registry=self.registry,
            events=self.events,
            llm=self.llm,
            settings=settings,
        )
        self.reports = ReportService(self.sessions, self.artifacts)
