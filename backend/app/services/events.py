from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import timezone
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.events import RunEvent, StoredRunEvent
from app.persistence.adapters import dump_run_event, load_run_event
from app.persistence.models import RunEventRow
from app.services.event_bus import EventBus


T = TypeVar("T")
Mutation = Callable[[AsyncSession], Awaitable[T]]


def stored_event_from_row(row: RunEventRow) -> StoredRunEvent:
    occurred_at = row.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return StoredRunEvent(
        event_id=row.id,
        sequence=row.sequence,
        occurred_at=occurred_at,
        event=load_run_event(
            event_type=row.type,
            test_run_id=row.test_run_id,
            task_run_id=row.task_run_id,
            payload=row.payload_json,
        ),
    )


class EventWriter:
    """Owns event sequence, deduplication, commit, then publication."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
    ) -> None:
        self.sessions = sessions
        self.event_bus = event_bus
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def append(
        self, event: RunEvent, *, dedup_key: str | None = None
    ) -> StoredRunEvent | None:
        _, stored = await self.commit(event, _nothing, dedup_key=dedup_key)
        return stored

    async def append_many(
        self, events: Sequence[tuple[RunEvent, str | None]]
    ) -> list[StoredRunEvent]:
        _, stored = await self.commit_many(events, _nothing)
        return stored

    async def commit(
        self,
        event: RunEvent,
        mutation: Mutation[T],
        *,
        dedup_key: str | None = None,
    ) -> tuple[T, StoredRunEvent | None]:
        values, stored = await self.commit_many(
            [(event, dedup_key)], mutation
        )
        return values, stored[0] if stored else None

    async def commit_many(
        self,
        events: Sequence[tuple[RunEvent, str | None]],
        mutation: Mutation[T],
    ) -> tuple[T, list[StoredRunEvent]]:
        if not events:
            raise ValueError("EventWriter.commit_many requires at least one event")
        run_id = events[0][0].test_run_id
        if any(event.test_run_id != run_id for event, _ in events):
            raise ValueError("one event transaction cannot span test runs")
        rows: list[RunEventRow] = []
        async with self._locks[run_id]:
            async with self.sessions() as session:
                value = await mutation(session)
                for event, dedup_key in events:
                    row = await self._stage(session, event, dedup_key=dedup_key)
                    if row is not None:
                        rows.append(row)
                await session.commit()
        stored = [stored_event_from_row(row) for row in rows]
        for row in rows:
            await self.event_bus.publish(row.test_run_id, row.sequence)
        return value, stored

    async def _stage(
        self,
        session: AsyncSession,
        event: RunEvent,
        *,
        dedup_key: str | None,
    ) -> RunEventRow | None:
        if dedup_key:
            existing = await session.scalar(
                select(RunEventRow.id).where(
                    RunEventRow.test_run_id == event.test_run_id,
                    RunEventRow.dedup_key == dedup_key,
                )
            )
            if existing is not None:
                return None
        current = await session.scalar(
            select(func.max(RunEventRow.sequence)).where(
                RunEventRow.test_run_id == event.test_run_id
            )
        )
        event_type, payload = dump_run_event(event)
        row = RunEventRow(
            id=str(uuid4()),
            sequence=(current or 0) + 1,
            test_run_id=event.test_run_id,
            task_run_id=event.task_run_id,
            type=event_type,
            payload_json=payload,
            dedup_key=dedup_key,
        )
        session.add(row)
        await session.flush()
        return row


async def _nothing(_: AsyncSession) -> None:
    return None
