from __future__ import annotations

from pathlib import Path

from backend.app.domain.models import DECISION_ADAPTER, DecisionContext, PlanOutput, PlanRequest


from openai import AsyncOpenAI
from pydantic import ValidationError


import base64
import json
from typing import Any


from app.domain.models import (
    Decision,
)


def _prompt(path: str) -> str:
    return (Path(__file__).resolve().parents[1] / "prompts" / path).read_text("utf-8")


class RealLLMProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0,
        timeout_seconds: float = 60,
        save_raw_response: bool = False,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("LLM_BASE_URL and LLM_API_KEY are required in real mode")
        self.client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_seconds
        )
        self.model = model
        self.temperature = temperature
        self.save_raw_response = save_raw_response
        self.model_info: dict[str, object] = {
            "provider": "openai-compatible",
            "base_url": base_url,
            "model": model,
            "temperature": temperature,
        }

    async def _structured(
        self,
        messages: list[dict[str, Any]],
        *,
        name: str,
        schema: dict[str, Any],
        validator: Any,
    ) -> Any:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": name,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Model returned no structured content")
                return validator(json.loads(content))
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
        raise ValueError(f"Structured output failed after 3 attempts: {last_error}")

    async def plan(self, request: PlanRequest) -> PlanOutput:
        messages = [
            {"role": "system", "content": _prompt("planner.txt")},
            {
                "role": "user",
                "content": json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
            },
        ]
        return await self._structured(
            messages,
            name="plan_output",
            schema=PlanOutput.model_json_schema(),
            validator=PlanOutput.model_validate,
        )

    async def decide(self, ctx: DecisionContext) -> Decision:
        image = base64.b64encode(ctx.screenshot_bytes).decode("ascii")
        payload = ctx.model_dump(
            mode="json", exclude={"screenshot_bytes", "screenshot_mime_type"}
        )
        messages = [
            {"role": "system", "content": _prompt("decision.txt")},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(payload, ensure_ascii=False),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{ctx.screenshot_mime_type};base64,{image}"
                        },
                    },
                ],
            },
        ]
        return await self._structured(
            messages,
            name="decision",
            schema=DECISION_ADAPTER.json_schema(),
            validator=DECISION_ADAPTER.validate_python,
        )