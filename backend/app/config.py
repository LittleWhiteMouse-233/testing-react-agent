"""应用配置家族、唯一文件加载及原子热更新。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import tomli_w

from app.domain.execution import ExecutionSettings
from app.domain.activity import AgentActivity
from app.domain.ids import ModelProfileId
from app.domain.resources import LLMProfileSettings
from app.test_case_lock import TestCaseLock


PROJECT_ROOT = Path(__file__).resolve().parents[2]


TProfile = TypeVar("TProfile", bound=LLMProfileSettings)


class LLMSettings(BaseModel, Generic[TProfile]):
    """模型集合及两个必选路由；配置、替换请求、公开响应共享结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    profiles: list[TProfile] = Field(min_length=1, title="模型配置列表", description="可以新增、编辑、删除模型；顺序不影响模型选择。")
    planning_model_id: ModelProfileId = Field(title="规划模型", description="生成测试计划使用的模型。", json_schema_extra={"x-options-path": ["llm", "profiles"], "x-option-value": "id"})
    execution_model_id: ModelProfileId = Field(title="执行模型", description="Act、Judge 和 Goal 共用的模型。", json_schema_extra={"x-options-path": ["llm", "profiles"], "x-option-value": "id"})

    @model_validator(mode="after")
    def model_routes_are_valid(self) -> LLMSettings[TProfile]:
        profile_ids = [profile.id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("Model profile ids must be unique")
        for route_name, model_id in (("planning_model_id", self.planning_model_id), ("execution_model_id", self.execution_model_id)):
            if model_id not in profile_ids:
                raise ValueError(f"{route_name} references unknown model")
        return self

    def profile_for_activity(self, activity: AgentActivity) -> TProfile:
        """按必选路由取得配置，调用方在工作准备阶段固定其副本。"""
        model_id = self.planning_model_id if activity == AgentActivity.PLANNING else self.execution_model_id
        return next(profile for profile in self.profiles if profile.id == model_id)


class SettingsContent(BaseModel, Generic[TProfile]):
    """配置内容的共同事实；根级启动参数与嵌套运行参数各只有一份。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    app_name: str = Field(title="应用名称", description="后端接口文档显示的应用名称。")
    app_version: str = Field(title="应用版本", description="后端及运行记录使用的应用版本。")
    debug: bool = Field(title="调试模式", description="后端调试开关。")
    cors_origins: list[str] = Field(title="允许的浏览器来源", description="允许访问后端的浏览器 Origin 列表。")
    data_dir: Path = Field(title="数据目录", description="数据库、图片和导出文件的默认根目录。")
    database_url: str | None = Field(default=None, title="数据库地址", description="留空时从数据目录派生 SQLite 地址。")
    checkpoint_path: Path | None = Field(default=None, title="检查点文件", description="留空时使用数据目录内的 checkpoints.db。")
    mcp_config_path: Path = Field(title="MCP 连接配置文件", description="独立外部服务的连接配置路径。")
    skills_dir: Path = Field(title="资料目录", description="本地 Skill 和静态资料的根目录。")
    execution: ExecutionSettings = Field(title="执行参数", description="新运行使用的预算和交互参数。", json_schema_extra={"x-requires-restart": False})
    llm: LLMSettings[TProfile] = Field(title="模型设置", description="新规划和新运行使用的模型。", json_schema_extra={"x-requires-restart": False})


class Settings(SettingsContent[LLMProfileSettings]):
    """绑定来源文件的完整应用配置；路径不写入 TOML，令牌保留。"""

    config_path: Path = Field(exclude=True)

    @field_validator("config_path")
    @classmethod
    def config_path_is_absolute(cls, config_path: Path) -> Path:
        if not config_path.is_absolute():
            raise ValueError("config_path must be an absolute path")
        return config_path

    @model_validator(mode="after")
    def credentials_are_configured(self) -> Settings:
        if any(profile.mode == "real" and not profile.api_key for profile in self.llm.profiles):
            raise ValueError("api_key is required for real model profiles")
        return self

    def model_post_init(self, __context: object) -> None:
        for field_name in ("mcp_config_path", "skills_dir", "data_dir", "checkpoint_path"):
            configured_path = getattr(self, field_name)
            if configured_path is not None and not configured_path.is_absolute():
                object.__setattr__(self, field_name, (PROJECT_ROOT / configured_path).resolve())

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


def get_settings(config_path: Path | None = None) -> Settings:
    """读取并绑定唯一实际配置；路径从仓库根目录解析，不创建目录。"""
    selected_path = config_path if config_path is not None else Path(os.environ.get("TEST_AGENT_CONFIG_PATH") or "config.toml")
    selected_path = (PROJECT_ROOT / selected_path).resolve()
    with selected_path.open("rb") as config_file:
        settings_values = tomllib.load(config_file)
    if "config_path" in settings_values:
        raise ValueError("config_path selects the source file and cannot be configured inside TOML")
    return Settings.model_validate({**settings_values, "config_path": selected_path})


class SettingsBusy(RuntimeError):
    """配置保存与规划或运行准备占用冲突。"""


class SettingsVersionConflict(ValueError):
    """请求基于旧配置或上一次进程的版本。"""


class SettingsService:
    """唯一配置发布者；已接受的保存拥有独立于 HTTP 调用者的生命周期。"""

    def __init__(self, settings: Settings, test_case_lock: TestCaseLock) -> None:
        self._current = settings
        self.test_case_lock = test_case_lock
        self.version = str(uuid4())
        self.restart_required = False
        self._save_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def current(self) -> Settings:
        return self._current

    def ensure_available(self) -> None:
        if self._save_task is not None or self._closing:
            raise SettingsBusy("设置正在保存或应用正在关闭，请稍后重试")

    async def update(self, content: SettingsContent[LLMProfileSettings], *, version: str) -> None:
        self.ensure_available()
        if self.test_case_lock.has_preparation():
            raise SettingsBusy("正在准备规划或运行，请稍后保存设置")
        if version != self.version:
            raise SettingsVersionConflict("配置已更新，请重新加载后保存")
        # Check and registration cannot yield on the application's single event loop.
        save_task = asyncio.create_task(self._save(content), name="save-settings")
        self._save_task = save_task
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            await save_task
            raise

    async def _save(self, content: SettingsContent[LLMProfileSettings]) -> None:
        temporary_path: Path | None = None
        try:
            previous_profiles = {profile.id: profile for profile in self._current.llm.profiles}
            candidate_values = content.model_dump()
            candidate_values["config_path"] = self._current.config_path
            for profile, profile_values in zip(content.llm.profiles, candidate_values["llm"]["profiles"], strict=True):
                previous_profile = previous_profiles.get(profile.id)
                profile_values["api_key"] = profile.api_key or (previous_profile.api_key if previous_profile else None)
            candidate = Settings.model_validate(candidate_values)
            startup_changed = any(
                getattr(candidate, field_name) != getattr(self._current, field_name)
                for field_name, field in SettingsContent[LLMProfileSettings].model_fields.items()
                if not isinstance(field.json_schema_extra, dict) or field.json_schema_extra.get("x-requires-restart", True)
            )
            file_values = candidate.model_dump(mode="json", exclude_none=True)
            for profile, profile_values in zip(candidate.llm.profiles, file_values["llm"]["profiles"], strict=True):
                if profile.api_key is not None:
                    profile_values["api_key"] = profile.api_key
            contents = tomli_w.dumps(file_values)
            next_version = str(uuid4())
            temporary_path = await asyncio.to_thread(self._write_temporary_file, contents)
            # No await between the file commit and the one in-memory publication.
            os.replace(temporary_path, candidate.config_path)
            self._current = candidate
            self.version = next_version
            self.restart_required = self.restart_required or startup_changed
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            self._save_task = None

    def _write_temporary_file(self, contents: str) -> Path:
        config_path = self._current.config_path
        config_file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=config_path.parent,
                                                  prefix=f".{config_path.name}.", suffix=".tmp", delete=False)
        temporary_path = Path(config_file.name)
        try:
            with config_file:
                config_file.write(contents)
                config_file.flush()
                os.fsync(config_file.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return temporary_path

    async def shutdown(self) -> None:
        self._closing = True
        if self._save_task is not None:
            await asyncio.gather(self._save_task, return_exceptions=True)
