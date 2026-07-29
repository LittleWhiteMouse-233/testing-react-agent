from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.persistence.models import StepEventRow
from app.services.event_bus import EventBus


def serialize_event(row: StepEventRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "sequence": row.sequence,
        "run_id": row.test_run_id,
        "task_run_id": row.task_run_id,
        "type": row.type,
        "timestamp": row.created_at.isoformat(),
        "payload": row.payload_json,
    }


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
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        task_run_id: str | None = None,
        dedup_key: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.sessions() as session:
            if dedup_key:
                existing = await session.scalar(
                    select(StepEventRow).where(
                        StepEventRow.test_run_id == run_id,
                        StepEventRow.dedup_key == dedup_key,
                    )
                )
                if existing:
                    return None
            current_sequence = await session.scalar(
                select(func.max(StepEventRow.sequence)).where(
                    StepEventRow.test_run_id == run_id
                )
            )
            sequence = (current_sequence if current_sequence is not None else 0) + 1
            row = StepEventRow(
                id=str(uuid4()),
                sequence=sequence,
                test_run_id=run_id,
                task_run_id=task_run_id,
                type=event_type,
                payload_json=payload,
                dedup_key=dedup_key,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if dedup_key:
                    return None
                raise
            await session.refresh(row)
        await self.event_bus.publish(run_id, sequence)
        return serialize_event(row)
