from __future__ import annotations

from functools import partial
from pathlib import Path

from app.api.repository import read_test_run_report
from app.artifacts import ArtifactStore
from app.llm import ModelProvider
from app.config import Settings, SettingsService, resolve_config_path
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
from app.resources.mcp import MCPClientProvider
from app.resources.static_resources import SkillResourceProvider
from app.resources.tools import ToolProvider
from app.test_case_lock import TestCaseLock


class Container:
    def __init__(self, settings: Settings, config_path: Path | None = None) -> None:
        self.test_case_lock = TestCaseLock()
        self.settings_service = SettingsService(settings, resolve_config_path(config_path), self.test_case_lock)
        self.engine = build_engine(settings.db_url)
        self.sessions = build_session_factory(self.engine)
        self.run_event_bus = EventBus[int]()
        self.planning_event_bus = EventBus[PlanningEvent]()
        self.run_event_writer = RunEventWriter(self.sessions, self.run_event_bus)
        self.artifacts = ArtifactStore(
            settings.artifacts_dir,
            self.sessions,
        )
        self.mcp_clients = MCPClientProvider(settings.mcp_config_path)
        self.skills = SkillResourceProvider(settings.skills_dir, self.mcp_clients)
        self.tools = ToolProvider(
            self.mcp_clients, additional_tools=(*self.skills.build_resource_tools(), *TASK_AGENT_TOOLS),
        )
        prompt_catalog = PromptCatalog()
        self.model_provider = ModelProvider()
        self.planning_graph = PlanningGraph(prompt_catalog.prompt_for_activity(AgentActivity.PLANNING))
        self.repository = SqlAlchemyRunRepository(self.sessions, self.run_event_writer)
        self.planning = PlanningService(
            planning_event_bus=self.planning_event_bus,
            test_case_lock=self.test_case_lock,
            repository=self.repository,
            planning_graph=self.planning_graph,
            model_provider=self.model_provider,
            settings_service=self.settings_service,
        )
        self.agent_factory = TaskAgentFactory(
            artifacts=self.artifacts,
            model_provider=self.model_provider,
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
            mcp_clients=self.mcp_clients,
            tools=self.tools,
            skills=self.skills,
        )
        self.reports = ReportService(partial(read_test_run_report, self.sessions), self.artifacts)
