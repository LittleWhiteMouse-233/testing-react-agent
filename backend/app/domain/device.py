from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeviceHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    message: str


class DeviceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None
    resolution: str | None
    locale: str | None


class DeviceEnvironmentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    info: DeviceInfo | None
    health: DeviceHealth
