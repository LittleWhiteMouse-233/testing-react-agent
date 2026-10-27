from __future__ import annotations

from functools import partial

from app.api.repository import read_test_run_report
from app.artifacts import ArtifactStore
from app.llm import (
    ChatModelClient,
    ModelProvider,
    RealChatModelClient,
    ScriptedChatModelClient,
)
from app.config import Settings
from app.domain.activity import AgentActivity
from app.event_stream import EventBus, RunEventWriter
from app.domain.planning.events import PlanningEvent
from app.domain.planning import TestTaskType
from app.execution.executor import RunExecutor
from app.execution.run_service import RunService
from app.execution.task_agent import TASK_AGENT_TOOLS, TaskAgentFactory
from app.persistence.db import build_engine, build_session_factory
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.planning import PlanningGraph, PlanningService
from app.prompts import PromptCatalog
from app.reporting import ReportService
from app.mcp import MCPClientProvider
from app.skill import SkillResourceProvider
from app.tools import ToolProvider
from app.test_case_lock import TestCaseLock


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
            execution_model_id=settings.execution_model_id,
        )
        self.planning_graph = PlanningGraph(prompt_catalog.prompt_for_activity(AgentActivity.PLANNING))
        self.repository = SqlAlchemyRunRepository(self.sessions, self.run_event_writer)
        self.test_case_lock = TestCaseLock()
        self.planning = PlanningService(
            planning_event_bus=self.planning_event_bus,
            test_case_lock=self.test_case_lock,
            repository=self.repository,
            planning_graph=self.planning_graph,
            model_provider=self.model_provider,
        )
        self.agent_factory = TaskAgentFactory(
            artifacts=self.artifacts,
            model_provider=self.model_provider,
            prompt_catalog=prompt_catalog,
            tool_call_timeout_seconds=settings.tool_call_timeout_seconds,
            model_call_max_attempts=settings.execution_model_call_max_attempts,
            model_response_max_attempts=settings.execution_model_response_max_attempts,
            image_history_rounds=settings.image_history_rounds,
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
            model_provider=self.model_provider,
            app_version=settings.app_version,
            prompt_catalog=prompt_catalog,
            mcp_clients=self.mcp_clients,
            tools=self.tools,
            skills=self.skills,
            discovery_timeout_seconds=settings.tool_call_timeout_seconds,
            max_cycles_by_task_type={
                TestTaskType.ACT: settings.act_max_cycles,
                TestTaskType.JUDGE: settings.judge_max_cycles,
                TestTaskType.GOAL: settings.goal_max_cycles,
            },
            goal_max_failures=settings.goal_max_failures,
        )
        self.reports = ReportService(partial(read_test_run_report, self.sessions), self.artifacts)
