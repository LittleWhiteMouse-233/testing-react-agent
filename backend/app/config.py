from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LLMProfileSettings(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    mode: Literal["scripted", "real"] = "scripted"
    base_url: str | None = None
    api_key: str | None = None
    model: str = "deterministic"
    temperature: float = 0
    timeout_seconds: float = Field(default=60, gt=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="TEST_AGENT_",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Android TV Test Agent"
    app_version: str = "0.1.0"
    debug: bool = False
    cors_origins: str = "http://localhost:5173"

    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str | None = None
    checkpoint_path: Path | None = None

    llm_profiles: list[LLMProfileSettings] = Field(
        default_factory=lambda: [LLMProfileSettings(id="default")],
        validation_alias="LLM_PROFILES",
    )
    planning_model_id: str | None = None
    act_model_id: str | None = None
    judge_model_id: str | None = None

    adb_path: str = Field(default="adb", validation_alias="ADB_PATH")
    adb_serial: str | None = Field(default=None, validation_alias="ADB_SERIAL")
    action_timeout_seconds: float = 15
    capture_max_attempts: int = Field(default=3, ge=1, le=10)
    model_call_max_attempts: int = Field(default=3, ge=1, le=10)
    model_response_max_attempts: int = Field(default=3, ge=1, le=10)
    agent_history_max_tokens: int = Field(
        default=8_000,
        ge=1_000,
        le=100_000,
        validation_alias="TEST_AGENT_HISTORY_MAX_TOKENS",
    )

    @model_validator(mode="after")
    def model_routes_are_valid(self) -> "Settings":
        if not self.llm_profiles:
            raise ValueError("LLM_PROFILES must contain at least one model")
        ids = [profile.id for profile in self.llm_profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("LLM_PROFILES ids must be unique")
        known = set(ids)
        for field_name in (
            "planning_model_id",
            "act_model_id",
            "judge_model_id",
        ):
            value = getattr(self, field_name)
            if value is not None and value not in known:
                raise ValueError(f"{field_name} references unknown model: {value}")
        return self

    def model_post_init(self, __context: object) -> None:
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

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
