"""设备资源的实时描述与运行创建时快照结构。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeviceHealth(BaseModel):
    """一次实时健康检查结果，由 DeviceProvider 产生并供 API/运行创建消费。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    message: str


class DeviceInfo(BaseModel):
    """可复用的设备客观描述，不包含连接凭据或运行生命周期状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None
    resolution: str | None
    locale: str | None


class DeviceEnvironmentSnapshot(BaseModel):
    """TestRun 创建时冻结的设备提供方、描述与健康状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    info: DeviceInfo | None
    health: DeviceHealth
