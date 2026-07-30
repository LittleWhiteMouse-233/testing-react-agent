from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="ATV_",
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

    llm_mode: str = Field(
        default="scripted",
        validation_alias=AliasChoices("LLM_MODE", "ATV_LLM_MODE"),
    )
    llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_BASE_URL", "ATV_LLM_BASE_URL"),
    )
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "ATV_LLM_API_KEY"),
    )
    llm_model: str = Field(
        default="gpt-4.1-mini",
        validation_alias=AliasChoices("LLM_MODEL", "ATV_LLM_MODEL"),
    )
    llm_temperature: float = Field(
        default=0,
        validation_alias=AliasChoices("LLM_TEMPERATURE", "ATV_LLM_TEMPERATURE"),
    )
    llm_timeout_seconds: float = Field(
        default=60,
        validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "ATV_LLM_TIMEOUT_SECONDS"),
    )
    llm_save_raw_response: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LLM_SAVE_RAW_RESPONSE", "ATV_LLM_SAVE_RAW_RESPONSE"
        ),
    )

    adb_path: str = Field(
        default="adb",
        validation_alias=AliasChoices("ADB_PATH", "ATV_ADB_PATH"),
    )
    adb_serial: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ADB_SERIAL", "ATV_ADB_SERIAL"),
    )
    action_timeout_seconds: float = 15
    agent_history_max_tokens: int = Field(default=8_000, ge=1_000, le=100_000)
    enabled_tools: str = Field(default="")

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

    @property
    def enabled_tool_names(self) -> list[str]:
        return [item.strip() for item in self.enabled_tools.split(",") if item.strip()]

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
