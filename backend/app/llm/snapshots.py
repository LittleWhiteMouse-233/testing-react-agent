from __future__ import annotations

from app.config import LLMProfileSettings
from app.domain.llm import LLMProfileSnapshot


def profile_snapshot_from_settings(
    settings: LLMProfileSettings,
) -> LLMProfileSnapshot:
    """The only deployment-settings to public audit snapshot conversion."""

    provider = "openai-compatible" if settings.mode == "real" else "scripted"
    return LLMProfileSnapshot(
        profile_id=settings.id,
        provider=provider,
        model=settings.model,
        base_url=settings.base_url if settings.mode == "real" else None,
        temperature=settings.temperature,
        timeout_seconds=settings.timeout_seconds,
    )
