from __future__ import annotations

from app.llm.contracts import ChatModelProvider, ModelActivity


class ModelRegistry:
    def __init__(
        self,
        models: dict[str, ChatModelProvider],
        *,
        planning_model_id: str | None = None,
        act_model_id: str | None = None,
        judge_model_id: str | None = None,
    ) -> None:
        if not models:
            raise ValueError("At least one model provider is required")
        self.models = models
        self.routes = {
            ModelActivity.PLANNING: planning_model_id,
            ModelActivity.ACT: act_model_id,
            ModelActivity.JUDGE: judge_model_id,
        }
        for activity, model_id in self.routes.items():
            if model_id is not None and model_id not in models:
                raise ValueError(
                    f"{activity.value} references unknown model: {model_id}"
                )

    @property
    def default_model_id(self) -> str:
        return next(iter(self.models))

    def model_id_for(self, activity: ModelActivity) -> str:
        return self.routes[activity] or self.default_model_id

    def get(self, model_id: str) -> ChatModelProvider:
        try:
            return self.models[model_id]
        except KeyError as exc:
            raise LookupError(f"Model is not registered: {model_id}") from exc

    def for_activity(self, activity: ModelActivity) -> ChatModelProvider:
        return self.get(self.model_id_for(activity))
