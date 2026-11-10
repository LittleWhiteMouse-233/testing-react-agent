from __future__ import annotations

from functools import partial

from app.api.repository import read_test_run_report
from app.artifacts import ArtifactStore
from app.config import Settings, SettingsService
from app.domain.activity import AgentActivity
from app.event_stream import EventBus, RunEventWriter
from app.domain.planning import PlanningEvent
from app.execution.executor import RunExecutor
from app.execution.run_service import RunService
from app.execution.task_agent import TASK_AGENT_TOOLS, TaskAgentFactory
from app.persistence.db import build_engine, build_session_factory
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.planning import PlanningGraph, PlanningService
from app.resources.prompts import PromptCatalog
from app.reporting import ReportService
from app.resources.capabilities import CapabilityProvider
from app.test_case_lock import TestCaseLock


class Container:
    def __init__(self, settings: Settings) -> None:
        self.test_case_lock = TestCaseLock()
        self.settings_service = SettingsService(settings, self.test_case_lock)
        self.engine = build_engine(settings.db_url)
        self.sessions = build_session_factory(self.engine)
        self.run_event_bus = EventBus[int]()
        self.planning_event_bus = EventBus[PlanningEvent]()
        self.run_event_writer = RunEventWriter(self.sessions, self.run_event_bus)
        self.artifacts = ArtifactStore(
            settings.artifacts_dir,
            self.sessions,
        )
        self.capabilities = CapabilityProvider(settings.mcp_config_path, settings.skills_dir, TASK_AGENT_TOOLS)
        prompt_catalog = PromptCatalog()
        self.planning_graph = PlanningGraph(prompt_catalog.prompt_for_activity(AgentActivity.PLANNING))
        self.repository = SqlAlchemyRunRepository(self.sessions, self.run_event_writer)
        self.planning = PlanningService(
            planning_event_bus=self.planning_event_bus,
            test_case_lock=self.test_case_lock,
            repository=self.repository,
            planning_graph=self.planning_graph,
            settings_service=self.settings_service,
        )
        self.agent_factory = TaskAgentFactory(
            artifacts=self.artifacts,
            prompt_catalog=prompt_catalog,
        )
        self.executor = RunExecutor(
            repository=self.repository,
            agent_factory=self.agent_factory,
            checkpoint_path=str(settings.checkpoints),
        )
        self.run_service = RunService(
            test_case_lock=self.test_case_lock,
            repository=self.repository,
            executor=self.executor,
            settings_service=self.settings_service,
            app_version=settings.app_version,
            prompt_catalog=prompt_catalog,
            capabilities=self.capabilities,
        )
        self.reports = ReportService(partial(read_test_run_report, self.sessions), self.artifacts)
