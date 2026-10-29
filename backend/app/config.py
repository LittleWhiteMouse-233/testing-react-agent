from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.planning import TestTaskType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LLMProfileSettings(BaseModel):
    """单个模型客户端的完整部署配置；可调参数由实际配置显式提供。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1, max_length=100)
    mode: Literal["scripted", "real"]
    base_url: str | None = None
    api_key: str | None = None
    model: str
    temperature: float
    timeout_seconds: float = Field(gt=0)
    context_window_tokens: int = Field(ge=2_048)
    max_output_tokens: int = Field(ge=1)
    characters_per_token: float = Field(gt=0)
    tokens_per_image: int = Field(ge=1)
    context_safety_margin_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def model_parameters_are_valid(self) -> "LLMProfileSettings":
        if self.mode == "real" and (not self.base_url or not self.api_key):
            raise ValueError("base_url and api_key are required for real model profiles")
        reserved = self.max_output_tokens + self.context_safety_margin_tokens
        if reserved >= self.context_window_tokens:
            raise ValueError(
                "max_output_tokens plus context_safety_margin_tokens must be "
                "smaller than context_window_tokens"
            )
        return self


class LLMSettings(BaseModel):
    """有序模型配置及规划、执行路由，共享同一启动配置生命周期。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    profiles: list[LLMProfileSettings] = Field(min_length=1)
    planning_model_id: str | None = None
    execution_model_id: str | None = None

    @model_validator(mode="after")
    def model_routes_are_valid(self) -> "LLMSettings":
        profile_ids = [profile.id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("Model profile ids must be unique")
        for route_name, model_id in (
            ("planning_model_id", self.planning_model_id),
            ("execution_model_id", self.execution_model_id),
        ):
            if model_id is not None and model_id not in profile_ids:
                raise ValueError(f"{route_name} references unknown model: {model_id}")
        return self


class ExecutionSettings(BaseModel):
    """应用启动时加载的执行参数；各类型轮次上限随后固定到运行快照。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tool_call_timeout_seconds: float = Field(gt=0)
    goal_max_failures: int = Field(ge=1)
    image_history_rounds: int = Field(ge=1)
    model_call_max_attempts: int = Field(ge=1, le=10)
    model_response_max_attempts: int = Field(ge=1, le=10)
    max_cycles_by_task_type: dict[TestTaskType, Annotated[int, Field(ge=1, le=100)]]

    @field_validator("max_cycles_by_task_type")
    @classmethod
    def task_types_are_complete(cls, cycle_limits: dict[TestTaskType, int]) -> dict[TestTaskType, int]:
        if set(cycle_limits) != set(TestTaskType):
            raise ValueError("max_cycles_by_task_type must contain exactly the registered task types")
        return cycle_limits


class Settings(BaseModel):
    """单份 TOML 的应用配置边界；构造及校验不加载其他文件或环境值。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    app_name: str
    app_version: str
    debug: bool
    cors_origins: list[str]
    data_dir: Path
    database_url: str | None = None
    checkpoint_path: Path | None = None
    mcp_config_path: Path
    skills_dir: Path
    execution: ExecutionSettings
    llm: LLMSettings

    def model_post_init(self, __context: object) -> None:
        if not self.mcp_config_path.is_absolute():
            self.mcp_config_path = (PROJECT_ROOT / self.mcp_config_path).resolve()
        if not self.skills_dir.is_absolute():
            self.skills_dir = (PROJECT_ROOT / self.skills_dir).resolve()
        if not self.data_dir.is_absolute():
            self.data_dir = (PROJECT_ROOT / self.data_dir).resolve()
        if self.checkpoint_path is not None and not self.checkpoint_path.is_absolute():
            self.checkpoint_path = (PROJECT_ROOT / self.checkpoint_path).resolve()

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{(self.data_dir / 'app.db').as_posix()}"

    @property
    def checkpoints(self) -> Path:
        return self.checkpoint_path or self.data_dir / "checkpoints.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints.parent.mkdir(parents=True, exist_ok=True)


def load_settings(config_path: Path | None = None) -> Settings:
    """读取唯一选定文件；路径环境变量只选择文件，不覆盖配置字段。"""
    selected_path = config_path if config_path is not None else Path(
        os.environ.get("TEST_AGENT_CONFIG_PATH") or "config.toml"
    )
    if not selected_path.is_absolute():
        selected_path = PROJECT_ROOT / selected_path
    with selected_path.open("rb") as config_file:
        return Settings.model_validate(tomllib.load(config_file))


@lru_cache
def get_settings() -> Settings:
    settings = load_settings()
    settings.ensure_directories()
    return settings
