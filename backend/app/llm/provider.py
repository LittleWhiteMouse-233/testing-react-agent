"""按 Agent activity 或不可变 snapshot profile ID 提供模型客户端。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.activity import AgentActivity
from app.llm.client import ChatModelClient


class ModelProvider:
    """拥有 activity 路由和 profile 客户端集合，不暴露可变原始字典。"""

    def __init__(
        self,
        model_clients_by_id: Mapping[str, ChatModelClient],
        *,
        planning_model_id: str | None = None,
        act_model_id: str | None = None,
        judge_model_id: str | None = None,
    ) -> None:
        if not model_clients_by_id:
            raise ValueError("At least one model client is required")
        self._model_clients_by_id = dict(model_clients_by_id)
        self._routes = {
            AgentActivity.PLANNING: planning_model_id,
            AgentActivity.ACT: act_model_id,
            AgentActivity.JUDGE: judge_model_id,
        }
        for activity, model_id in self._routes.items():
            if model_id is not None and model_id not in self._model_clients_by_id:
                raise ValueError(
                    f"{activity.value} references unknown model: {model_id}"
                )

    @property
    def default_model_id(self) -> str:
        """返回配置有序列表中的首个 profile ID。"""

        return next(iter(self._model_clients_by_id))

    def model_id_for_activity(self, activity: AgentActivity) -> str:
        """按显式单值路由选择 profile，未配置时使用有序首项。"""

        return self._routes[activity] or self.default_model_id

    def client_by_id(self, model_id: str) -> ChatModelClient:
        """按 TestPlan/TestRun snapshot 固定的 profile ID 解析客户端。"""

        try:
            return self._model_clients_by_id[model_id]
        except KeyError as exc:
            raise LookupError(f"Model client is not registered: {model_id}") from exc

    def client_for_activity(self, activity: AgentActivity) -> ChatModelClient:
        """在创建计划或运行快照时按当前 activity 路由选择一次客户端。"""

        return self.client_by_id(self.model_id_for_activity(activity))

    def replace_client_for_testing(
        self, model_id: str, model_client: ChatModelClient
    ) -> None:
        """测试专用的受约束替换点：不得新增 ID 或改变 activity 路由。"""

        if model_id not in self._model_clients_by_id:
            raise LookupError(f"Model client is not registered: {model_id}")
        if model_client.model_id != model_id:
            raise ValueError("replacement client must preserve the registered model ID")
        self._model_clients_by_id[model_id] = model_client

    def replace_activity_route_for_testing(
        self, activity: AgentActivity, model_id: str
    ) -> None:
        """测试专用受约束路由替换点，用于证明已建 snapshot 不受后续路由影响。"""

        if model_id not in self._model_clients_by_id:
            raise LookupError(f"Model client is not registered: {model_id}")
        self._routes[activity] = model_id
