from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.events import (
    EXECUTION_EVENT_ADAPTER,
    ExecutionEvent,
    StoredExecutionEvent,
)
from app.persistence.models import StepEventRow
from app.services.event_bus import EventBus


def _event_payload(event: ExecutionEvent) -> dict[str, Any]:
    data = event.model_dump(mode="json")
    return {
        key: value
        for key, value in data.items()
        if key not in {"type", "run_id", "task_run_id"}
    }


def deserialize_event(row: StepEventRow) -> StoredExecutionEvent:
    event = EXECUTION_EVENT_ADAPTER.validate_python(
        {
            "type": row.type,
            "run_id": row.test_run_id,
            "task_run_id": row.task_run_id,
            **row.payload_json,
        }
    )
    return StoredExecutionEvent(
        id=row.id,
        sequence=row.sequence,
        timestamp=row.created_at,
        event=event,
    )


def serialize_event(row: StepEventRow) -> dict[str, Any]:
    return deserialize_event(row).to_api_dict()


class EventWriter:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
    ) -> None:
        self.sessions = sessions
        self.event_bus = event_bus

    async def append(
        self,
        event: ExecutionEvent,
        *,
        dedup_key: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await self.stage(
                session,
                event,
                dedup_key=dedup_key,
            )
            if row is None:
                return None
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if dedup_key:
                    return None
                raise
            await session.refresh(row)
        await self.publish(row)
        return serialize_event(row)

    async def stage(
        self,
        session: AsyncSession,
        event: ExecutionEvent,
        *,
        dedup_key: str | None = None,
    ) -> StepEventRow | None:
        if dedup_key:
            existing = await session.scalar(
                select(StepEventRow).where(
                    StepEventRow.test_run_id == event.run_id,
                    StepEventRow.dedup_key == dedup_key,
                )
            )
            if existing:
                return None
        current_sequence = await session.scalar(
            select(func.max(StepEventRow.sequence)).where(
                StepEventRow.test_run_id == event.run_id
            )
        )
        sequence = (current_sequence if current_sequence is not None else 0) + 1
        row = StepEventRow(
            id=str(uuid4()),
            sequence=sequence,
            test_run_id=event.run_id,
            task_run_id=event.task_run_id,
            type=event.type,
            payload_json=_event_payload(event),
            dedup_key=dedup_key,
        )
        session.add(row)
        await session.flush()
        return row

    async def publish(self, row: StepEventRow) -> None:
        await self.event_bus.publish(row.test_run_id, row.sequence)
